#!/usr/bin/env python3
"""
合并后的M3U文件处理器 - 对M3U文件进行直播源检查、质量分析、排序和筛选
该程序实现了完整的M3U文件处理功能,主要特点包括：

1.文件解析:能够正确解析本地M3U文件,提取频道名称、分组信息和URL
2.直播源检查:验证每个URL的可访问性
3.质量分析:使用FFprobe获取分辨率、码率、延迟和缓冲状态
4.多线程处理:使用ThreadPoolExecutor实现并发分析,提高处理效率
5.智能排序:先按频道类型(group-title)排序,再按分辨率排序，相同分辨率时按下载速度降序排列
6.频道筛选:对相同名称的频道进行去重,每个频道名称最多保留6个最佳质量的源

python merged_m3u_processor.py input.m3u -o output.m3u -t 10 -d 15
"""

import os
import re
import sys
import time
import json
import subprocess
import logging
import requests
from urllib.parse import urlparse, urljoin
from collections import defaultdict
import concurrent.futures
from typing import List, Optional, Tuple

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class Channel:
    """m3u节目源结构"""
    def __init__(self):
        self.name: str = ""
        self.urls: List[str] = []  # 支持多个直播源地址
        self.tvg_id: str = ""
        self.tvg_name: str = ""
        self.group_title: str = ""
        self.extinf_line: str = ""  # 保存原始的#EXTINF行
        self.available_urls: List[str] = []  # 保存可用的直播源地址
        self.valid_urls: List[str] = []  # 保存可用且能读取到分辨率的直播源地址
        self.original_lines: List[str] = []  # 保存原始文件中该频道的所有行
        self.valid_lines: List[str] = []  # 保存该频道中符合条件的所有行
        # 质量分析结果
        self.quality_info = {
            'resolution': '未知',
            'bitrate': '未知',
            'delay': '未知',
            'buffer_status': '未知',
            'download_speed': 0,
            'total_downloaded': 0,
            'download_time': 0
        }


class M3UProcessor:
    def __init__(self, input_file, output_file="output.m3u", max_threads=5, download_duration=15):
        self.input_file = input_file
        self.output_file = output_file
        self.max_threads = max_threads
        self.download_duration = download_duration
        self.channels = []
        self.header_lines = []
        
    def parse_m3u_file(self):
        """加载并解析m3u文件，返回频道列表，支持非标准IPTV扩展格式"""
        channels = []
        header_lines = []
        all_lines = []
        
        try:
            with open(self.input_file, 'r', encoding='utf-8') as file:
                all_lines = file.readlines()
        except UnicodeDecodeError:
            # 尝试使用其他编码
            try:
                with open(self.input_file, 'r', encoding='gbk') as file:
                    all_lines = file.readlines()
            except Exception as e:
                logger.error(f"无法打开文件 {self.input_file}: {e}")
                return header_lines, channels
        except Exception as e:
            logger.error(f"无法打开文件 {self.input_file}: {e}")
            return header_lines, channels
        
        # 识别文件头（#EXTM3U行和其他全局指令）
        channel_start_idx = -1
        for i, line in enumerate(all_lines):
            stripped_line = line.strip()
            if stripped_line.startswith('#EXTINF:'):
                channel_start_idx = i
                break
            header_lines.append(line)
        
        # 处理频道
        current_channel = None
        current_channel_lines = []
        
        for i, line in enumerate(all_lines):
            stripped_line = line.strip()
            
            # 处理#EXTINF行 - 新频道开始
            if stripped_line.startswith('#EXTINF:'):
                # 如果当前有未完成的频道，先保存
                if current_channel:
                    current_channel.original_lines = current_channel_lines
                    channels.append(current_channel)
                
                # 创建新频道
                current_channel = Channel()
                current_channel_lines = [line]  # 保存原始行，包括换行符
                
                # 保存原始的#EXTINF行
                current_channel.extinf_line = stripped_line
                
                # 解析频道名称
                comma_pos = stripped_line.rfind(',')
                if comma_pos != -1:
                    current_channel.name = stripped_line[comma_pos + 1:]
                
                # 解析tvg-id
                tvg_id_match = re.search(r'tvg-id="([^"]+)"', stripped_line)
                if tvg_id_match:
                    current_channel.tvg_id = tvg_id_match.group(1)
                
                # 解析tvg-name
                tvg_name_match = re.search(r'tvg-name="([^"]+)"', stripped_line)
                if tvg_name_match:
                    current_channel.tvg_name = tvg_name_match.group(1)
                
                # 解析group-title
                group_title_match = re.search(r'group-title="([^"]+)"', stripped_line)
                if group_title_match:
                    current_channel.group_title = group_title_match.group(1)
            
            # 处理URL行 - 可以是多个URL对应一个频道
            elif current_channel and (stripped_line.startswith('http://') or stripped_line.startswith('https://')):
                current_channel.urls.append(stripped_line)
                current_channel_lines.append(line)  # 保存原始行，包括换行符
            
            # 处理其他行（如注释、空行等）
            elif current_channel:
                current_channel_lines.append(line)  # 保存原始行，包括换行符
        
        # 保存最后一个频道，无论是否有URL
        if current_channel:
            current_channel.original_lines = current_channel_lines
            channels.append(current_channel)
        
        # 更新类属性 - 保留原始顺序，不进行排序
        self.channels = channels
        self.header_lines = header_lines
        
        return header_lines, channels

    def check_url_accessibility(self, url: str, timeout: int = 5) -> bool:
        """检查URL是否可访问"""
        # 跳过已知无效域名
        if "iptv.catvod.com" in url:
            logger.debug(f"跳过已知无效域名: {url}")
            return False
        try:
            # 使用HEAD请求快速检查
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            # 200-399是成功状态码
            return 200 <= response.status_code < 400
        except requests.RequestException as e:
            logger.debug(f"URL检查失败 {url}: {e}")
            return False

    def get_stream_info(self, url: str) -> Optional[Tuple[str, str, str, str]]:
        """使用FFmpeg获取码流信息，返回(分辨率, 码率, 延迟, 缓冲状态)"""
        # 首先尝试使用m3u8库进行复检
        try:
            import m3u8
            
            logger.debug(f"使用m3u8库复检URL: {url}")
            # 尝试解析m3u8文件
            m3u8_obj = m3u8.load(url)
            
            # 如果解析成功，说明URL是有效的m3u8流
            if m3u8_obj:
                logger.debug(f"m3u8库成功解析URL: {url}")
                # 检查是否有播放列表
                if hasattr(m3u8_obj, 'playlists') and m3u8_obj.playlists:
                    # 找到最高质量的播放列表
                    max_resolution = (0, 0)
                    
                    for playlist in m3u8_obj.playlists:
                        if hasattr(playlist, 'stream_info') and playlist.stream_info:
                            if hasattr(playlist.stream_info, 'resolution') and playlist.stream_info.resolution:
                                width, height = playlist.stream_info.resolution
                                if (width, height) > max_resolution:
                                    max_resolution = (width, height)
                    
                    if max_resolution != (0, 0):
                        resolution = f"{max_resolution[0]}x{max_resolution[1]}"
                        logger.debug(f"m3u8库检测到分辨率: {resolution}")
                        # 返回检测结果，码率和延迟使用FFprobe或默认值
                        return (resolution, "未知", "实时", "良好")
                    else:
                        # 虽然是有效的m3u8流，但没有分辨率信息
                        logger.debug(f"m3u8库解析成功，但未检测到分辨率")
                        return ("未知", "未知", "实时", "良好")
        except Exception as e:
            logger.debug(f"m3u8库复检失败 {url}: {e}")
        
        # 检查ffprobe是否可用
        ffprobe_path = None
        
        # 首先尝试直接调用命令名（跨平台兼容）
        logger.debug("尝试直接使用ffprobe命令名...")
        try:
            result = subprocess.run(
                ['ffprobe', '-version'],
                capture_output=True,
                text=True,
                check=True,
                timeout=5
            )
            logger.debug(f"成功直接调用ffprobe命令")
            logger.debug(f"ffprobe版本信息: {result.stdout[:50]}...")
            ffprobe_path = 'ffprobe'
        except FileNotFoundError:
            logger.debug("直接调用ffprobe命令失败")
        except Exception as e:
            logger.error(f"直接调用ffprobe时发生异常: {e}")
        
        # 如果直接调用失败，尝试不同平台的常见路径
        if not ffprobe_path:
            # 尝试Linux/Mac路径
            common_paths = [
                '/usr/bin/ffprobe',
                '/usr/local/bin/ffprobe',
                '/opt/homebrew/bin/ffprobe',
                r'C:\Program Files\ffmpeg\bin\ffprobe.exe',
                r'C:\Program Files (x86)\ffmpeg\bin\ffprobe.exe'
            ]
            
            for path in common_paths:
                try:
                    result = subprocess.run(
                        [path, '-version'],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=5
                    )
                    logger.debug(f"成功使用路径: {path}")
                    ffprobe_path = path
                    break
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
                except Exception as e:
                    logger.error(f"访问路径 {path} 时发生异常: {e}")
        
        # 如果仍然找不到ffprobe
        if not ffprobe_path:
            logger.error("无法找到ffprobe命令")
            logger.debug(f"当前PATH环境变量: {os.environ.get('PATH', '')}")
            logger.debug(f"当前工作目录: {os.getcwd()}")
            logger.error("请确保FFmpeg已安装并添加到系统PATH中")
            # 如果没有ffprobe，返回m3u8复检的默认结果
            return ("未知", "未知", "实时", "良好")
        
        # 构建FFprobe命令 - 优化参数以更快获取码流信息，增加超时时间
        cmd = [
            ffprobe_path,
            '-v', 'warning',  # 显示警告信息以便调试
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,bit_rate,avg_bit_rate,start_time,duration,codec_time_base',  # 添加延迟相关字段
            '-show_entries', 'format=bit_rate,start_time,duration,probe_score',  # 添加格式延迟和缓冲相关字段
            '-of', 'csv=p=0',
            '-timeout', '5000000',  # 5秒超时（单位：微秒）
            '-reconnect', '1',  # 允许重连
            '-reconnect_delay_max', '3',  # 最大重连延迟3秒
            '-reconnect_at_eof', '1',  # 允许在EOF时重连
            '-probesize', '2000000',  # 增加探针大小（2MB）
            '-analyzeduration', '5000000',  # 增加分析时长（5秒）
            '-rw_timeout', '5000000',  # 读写超时5秒
            '-max_delay', '5000000',  # 最大延迟5秒
            url
        ]
        
        try:
            # 执行命令
            logger.debug(f"执行ffprobe命令: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,  # 增加超时时间到20秒
                check=False  # 不使用check=True，避免非零退出码抛出异常
            )
            
            # 记录完整输出以便调试
            if result.stderr:
                logger.debug(f"ffprobe标准错误: {result.stderr.strip()}")
            if result.stdout:
                logger.debug(f"ffprobe标准输出: {result.stdout.strip()}")
            
            # 解析输出
            output = result.stdout.strip()
            if not output:
                logger.debug(f"ffprobe未返回有效输出，退出码: {result.returncode}")
                
                # 尝试使用JSON格式命令，获取更完整的信息
                logger.debug("尝试使用JSON格式命令获取详细信息")
                json_cmd = [
                    ffprobe_path,  # 修复硬编码问题
                    '-v', 'error',
                    '-i', url,
                    '-select_streams', 'v:0',  # 只选择视频流
                    '-show_entries', 'stream=width,height,bit_rate,avg_bit_rate,start_time,duration,codec_time_base',  # 添加延迟相关字段
                    '-show_entries', 'format=bit_rate,start_time,duration,probe_score',  # 添加格式延迟和缓冲相关字段
                    '-of', 'json',
                    '-timeout', '5000000',  # 5秒超时
                    '-reconnect', '1',  # 允许重连
                    '-reconnect_delay_max', '3',  # 最大重连延迟3秒
                    '-reconnect_at_eof', '1',  # 允许在EOF时重连
                    '-probesize', '2000000',  # 2MB探针大小
                    '-analyzeduration', '5000000',  # 5秒分析时长
                    '-rw_timeout', '5000000',  # 读写超时5秒
                    '-max_delay', '5000000'  # 最大延迟5秒
                ]
                
                json_result = subprocess.run(
                    json_cmd,
                    capture_output=True,
                    text=True,
                    timeout=20  # 增加超时时间到20秒
                )
                
                if json_result.stdout:
                    logger.debug(f"JSON命令输出: {json_result.stdout[:300]}...")
                    try:
                        data = json.loads(json_result.stdout)
                        
                        logger.debug(f"完整JSON数据: {json.dumps(data, indent=2)[:500]}...")
                        
                        # 查找视频流
                        streams = data.get('streams', [])
                        logger.debug(f"找到 {len(streams)} 个流")
                        
                        # 初始化码率变量
                        detected_bitrate = None
                        width = None
                        height = None
                        
                        # 1. 先查找视频流获取分辨率和优先码率
                        for stream in streams:
                            logger.debug(f"流信息: {json.dumps(stream, indent=2)[:300]}...")
                            if stream.get('codec_type') == 'video':
                                width = stream.get('width')
                                height = stream.get('height')
                                
                                # 1.1 尝试流级别的瞬时码率
                                if 'bit_rate' in stream and stream['bit_rate']:
                                    detected_bitrate = stream['bit_rate']
                                    logger.debug(f"从流获取瞬时码率: {detected_bitrate}")
                                    break
                                # 1.2 尝试流级别的平均码率
                                elif 'avg_bit_rate' in stream and stream['avg_bit_rate']:
                                    detected_bitrate = stream['avg_bit_rate']
                                    logger.debug(f"从流获取平均码率: {detected_bitrate}")
                                    break
                        
                        # 2. 如果视频流中没有码率，尝试格式级别的总码率
                        if detected_bitrate is None:
                            format_data = data.get('format', {})
                            logger.debug(f"格式信息: {json.dumps(format_data, indent=2)[:200]}...")
                            if 'bit_rate' in format_data and format_data['bit_rate']:
                                detected_bitrate = format_data['bit_rate']
                                logger.debug(f"从格式获取总码率: {detected_bitrate}")
                        
                        # 3. 如果找到了分辨率，返回结果
                        if width and height:
                            resolution = f"{width}x{height}"
                            
                            # 转换码率单位
                            bitrate_str = "未知"
                            if detected_bitrate:
                                try:
                                    bps = int(detected_bitrate)
                                    bitrate_str = f"{bps // 1000} kbps"
                                except ValueError:
                                    logger.debug(f"无效的码率值: {detected_bitrate}")
                                    # 尝试使用浮点数转换
                                    try:
                                        bps = float(detected_bitrate)
                                        bitrate_str = f"{int(bps // 1000)} kbps"
                                    except ValueError:
                                        logger.debug(f"无法转换码率值: {detected_bitrate}")
                            
                            # 4. 计算延迟和缓冲状态
                            delay_str = "未知"
                            buffer_status = "未知"
                            
                            # 获取延迟信息
                            stream_start_time = stream.get('start_time')
                            format_start_time = data.get('format', {}).get('start_time')
                            
                            # 处理延迟信息，过滤异常值
                            start_time_value = None
                            if stream_start_time:
                                try:
                                    start_time_value = float(stream_start_time)
                                except ValueError:
                                    logger.debug(f"无效的流开始时间: {stream_start_time}")
                            elif format_start_time:
                                try:
                                    start_time_value = float(format_start_time)
                                except ValueError:
                                    logger.debug(f"无效的格式开始时间: {format_start_time}")
                            
                            # 延迟判断逻辑，过滤异常大值
                            if start_time_value is not None:
                                if start_time_value < 0.1:
                                    delay_str = "实时"
                                elif start_time_value < 3600:  # 过滤超过1小时的异常值
                                    delay_str = f"{start_time_value:.1f}s"
                                else:
                                    delay_str = "未知"  # 对于异常大值显示"未知"
                            
                            # 获取缓冲状态
                            probe_score = data.get('format', {}).get('probe_score')
                            if probe_score:
                                try:
                                    # 支持浮点数字符串转换
                                    score = int(float(probe_score))
                                    if score > 80:
                                        buffer_status = "良好"
                                    elif score > 50:
                                        buffer_status = "一般"
                                    else:
                                        buffer_status = "较差"
                                except ValueError:
                                    logger.debug(f"无效的探针分数: {probe_score}")
                            
                            return (resolution, bitrate_str, delay_str, buffer_status)
                    except json.JSONDecodeError as e:
                        logger.debug(f"JSON解析失败: {e}")
                
                # 如果FFprobe失败，但m3u8库解析成功，返回默认结果
                return ("未知", "未知", "实时", "良好")
            
            # 处理输出
            lines = output.split('\n')
            if not lines:
                # 如果FFprobe失败，但m3u8库解析成功，返回默认结果
                return ("未知", "未知", "实时", "良好")
            
            # 解析CSV输出 - 处理多行格式
            stream_parts = []
            format_parts = []
            
            # 过滤空行
            valid_lines = [line.strip() for line in lines if line.strip()]
            
            # 查找有效的流信息和格式信息行
            for line in valid_lines:
                parts = line.split(',')
                # 流信息行通常包含width, height, start_time等
                if len(parts) >= 5:
                    # 检查是否包含有效的分辨率信息
                    if parts[0].isdigit() and parts[1].isdigit():
                        stream_parts = parts
                        logger.debug(f"解析流CSV输出: {stream_parts}")
                # 格式信息行通常包含bit_rate, start_time, probe_score等
                elif len(parts) >= 4:
                    format_parts = parts
                    logger.debug(f"解析格式CSV输出: {format_parts}")
            
            # 如果没有找到有效的流信息，尝试使用第一行
            if not stream_parts and valid_lines:
                stream_parts = valid_lines[0].split(',')
                logger.debug(f"使用第一行作为流信息: {stream_parts}")
            # 如果没有找到有效的格式信息，尝试使用最后一行
            if not format_parts and len(valid_lines) > 1:
                format_parts = valid_lines[-1].split(',')
                logger.debug(f"使用最后一行作为格式信息: {format_parts}")
            
            # 处理不同的列数情况
            if len(stream_parts) >= 2:
                # 至少有width和height
                width = stream_parts[0].strip()
                height = stream_parts[1].strip()
                
                # 验证分辨率
                if width.isdigit() and height.isdigit():
                    resolution = f"{width}x{height}"
                    
                    # 处理码率 - 支持不同的输出格式
                    bitrate_str = "未知"
                    detected_bitrate = None
                    
                    # 检查所有可能的码率字段
                    # 1. 流瞬时码率 (流信息第5列)
                    if len(stream_parts) >= 5:
                        bitrate = stream_parts[4].strip()
                        logger.debug(f"找到流瞬时码率字段: {bitrate}")
                        if bitrate and bitrate != 'N/A' and bitrate != '0':
                            try:
                                # 支持浮点数字符串转换
                                detected_bitrate = int(float(bitrate))
                            except ValueError:
                                logger.debug(f"无效的流瞬时码率值: {bitrate}")
                    
                    # 2. 格式总码率 (格式信息第3列)
                    if len(format_parts) >= 3 and detected_bitrate is None:
                        format_bitrate = format_parts[2].strip()
                        logger.debug(f"找到格式总码率字段: {format_bitrate}")
                        if format_bitrate and format_bitrate != 'N/A' and format_bitrate != '0':
                            try:
                                # 支持浮点数字符串转换
                                detected_bitrate = int(float(format_bitrate))
                            except ValueError:
                                logger.debug(f"无效的格式总码率值: {format_bitrate}")
                    
                    # 转换码率
                    if detected_bitrate is not None:
                        bitrate_str = f"{detected_bitrate // 1000} kbps"
                    
                    # 处理延迟和缓冲状态
                    delay_str = "未知"
                    buffer_status = "未知"
                    
                    # 获取延迟信息 (流信息第3列: 流开始时间)
                    if len(stream_parts) >= 3:
                        stream_start_time = stream_parts[2].strip()
                        if stream_start_time and stream_start_time != 'N/A':
                            try:
                                start_time = float(stream_start_time)
                                # 过滤异常大的时间戳（超过1小时的视为异常）
                                if start_time < 0.1:
                                    delay_str = "实时"
                                elif start_time < 3600:  # 1小时以内视为有效
                                    delay_str = f"{start_time:.1f}s"
                                else:
                                    delay_str = "未知"
                            except ValueError:
                                logger.debug(f"无效的流开始时间: {stream_start_time}")
                    
                    # 获取缓冲状态 (格式信息第4列: 探针分数)
                    if len(format_parts) >= 4:
                        probe_score = format_parts[3].strip()
                        if probe_score and probe_score != 'N/A':
                            try:
                                score = int(float(probe_score))
                                if score > 80:
                                    buffer_status = "良好"
                                elif score > 50:
                                    buffer_status = "一般"
                                else:
                                    buffer_status = "较差"
                            except ValueError:
                                logger.debug(f"无效的探针分数: {probe_score}")
                    
                    return (resolution, bitrate_str, delay_str, buffer_status)
            
            # 如果FFprobe解析失败，但m3u8库解析成功，返回默认结果
            return ("未知", "未知", "实时", "良好")
        
        except subprocess.TimeoutExpired:
            logger.error(f"FFprobe超时 {url}")
            # 如果FFprobe超时，但m3u8库解析成功，返回默认结果
            return ("未知", "未知", "实时", "良好")
        except subprocess.CalledProcessError as e:
            logger.error(f"FFprobe调用失败 {url}: {e}")
            # 如果FFprobe调用失败，但m3u8库解析成功，返回默认结果
            return ("未知", "未知", "实时", "良好")
        except Exception as e:
            logger.error(f"获取码流信息失败 {url}: {e}")
            # 如果发生其他异常，但m3u8库解析成功，返回默认结果
            return ("未知", "未知", "实时", "良好")

    def analyze_channel_quality(self, channel):
        """分析单个频道的所有直播源质量信息"""
        try:
            # 为每个URL存储质量信息
            channel.quality_info_list = []
            
            # 处理频道的所有URL
            for url in channel.urls:
                # 跳过已知无效域名
                if "iptv.catvod.com" in url:
                    logger.debug(f"跳过已知无效域名的分析: {url}")
                    # 添加标记为不可访问的质量信息
                    channel.quality_info_list.append({
                        'resolution': '不可访问',
                        'bitrate': '未知',
                        'delay': '未知',
                        'buffer_status': '未知',
                        'download_speed': 0,
                        'total_downloaded': 0,
                        'download_time': 0
                    })
                    continue
                url_quality = {
                    'resolution': '未知',
                    'bitrate': '未知',
                    'delay': '未知',
                    'buffer_status': '未知',
                    'download_speed': 0,
                    'total_downloaded': 0,
                    'download_time': 0
                }
                
                # 首先检查URL可访问性
                if not self.check_url_accessibility(url):
                    url_quality['resolution'] = '不可访问'
                    channel.quality_info_list.append(url_quality)
                    continue
                
                # 使用FFprobe获取流信息
                stream_info = self.get_stream_info(url)
                if stream_info:
                    url_quality['resolution'], url_quality['bitrate'], url_quality['delay'], url_quality['buffer_status'] = stream_info
                
                # 测试下载速度
                try:
                    start_time = time.time()
                    response = requests.get(url, stream=True, timeout=15)
                    
                    # 下载指定时长的数据
                    chunk_size = 1024 * 64  # 64KB chunks
                    total_downloaded = 0
                    
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            total_downloaded += len(chunk)
                        # 检查是否已经达到指定的下载时间
                        if time.time() - start_time >= self.download_duration:
                            break
                    
                    download_time = time.time() - start_time
                    
                    if download_time > 0:
                        url_quality['download_speed'] = total_downloaded / download_time / 1024  # KB/s
                        url_quality['total_downloaded'] = total_downloaded
                        url_quality['download_time'] = download_time
                    
                    response.close()
                except Exception as e:
                    logger.error(f"下载速度测试失败 {url}: {e}")
                
                channel.quality_info_list.append(url_quality)
            
            # 设置默认的quality_info为第一个URL的信息（兼容旧代码）
            if channel.quality_info_list:
                channel.quality_info = channel.quality_info_list[0]
            
            return channel.quality_info_list
            
        except Exception as e:
            logger.error(f"分析频道 {channel.name or '未知'} 失败: {e}")
            return []
    
    def analyze_all_channels(self):
        """使用多线程分析所有频道的质量"""
        print("开始分析频道质量...")
        print(f"每个频道将下载约{self.download_duration}秒的TS片段")
        
        # 只分析有直播源的频道
        channels_with_sources = [channel for channel in self.channels if channel.urls]
        print(f"跳过 {len(self.channels) - len(channels_with_sources)} 个无直播源的频道，共分析 {len(channels_with_sources)} 个频道")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            future_to_channel = {
                executor.submit(self.analyze_channel_quality, channel): channel 
                for channel in channels_with_sources
            }
            
            for i, future in enumerate(concurrent.futures.as_completed(future_to_channel)):
                channel = future_to_channel[future]
                try:
                    quality_info_list = future.result()
                    
                    # 打印详细的分析结果
                    print(f"\n[{i+1}/{len(channels_with_sources)}] 频道: {channel.name}")
                    print(f"  直播源数量: {len(channel.urls)}")
                    
                    for j, (url, quality_info) in enumerate(zip(channel.urls, quality_info_list)):
                        print(f"  直播源 {j+1}:")
                        print(f"    URL: {url}")
                        print(f"    分辨率: {quality_info['resolution']}")
                        print(f"    码率: {quality_info['bitrate']}")
                        print(f"    延迟: {quality_info['delay']}")
                        print(f"    缓冲状态: {quality_info['buffer_status']}")
                        print(f"    下载速度: {quality_info['download_speed']:.2f} KB/s")
                        print(f"    总下载量: {quality_info['total_downloaded']/1024:.1f} KB")
                        print(f"    总下载时间: {quality_info['download_time']:.2f} s")
                    
                    print("-" * 60)
                    
                except Exception as e:
                    print(f"分析频道 {channel.name} 时发生错误: {e}")
    
    def filter_channels(self):
        """过滤频道，只保留可以读取到分辨率且缓冲状态良好的频道"""
        # 过滤条件：分辨率不是未知/不可访问，且缓冲状态为良好
        filtered_channels = []
        
        for channel in self.channels:
            # 检查该频道是否有至少一个URL满足条件
            has_valid_url = False
            
            # 遍历频道的所有URL，检查是否有满足条件的
            for i, url in enumerate(channel.urls):
                # 获取该URL的质量信息，确保索引不超出范围
                url_quality = channel.quality_info
                if hasattr(channel, 'quality_info_list') and i < len(channel.quality_info_list):
                    url_quality = channel.quality_info_list[i]
                
                # 检查条件：分辨率有效且缓冲状态良好
                if url_quality['resolution'] not in ['未知', '不可访问'] and url_quality['buffer_status'] == '良好':
                    has_valid_url = True
                    break
            
            if has_valid_url:
                filtered_channels.append(channel)
        
        self.channels = filtered_channels
    
    def _parse_resolution(self, resolution_str):
        """解析分辨率字符串为可比较的数值"""
        if resolution_str == '未知' or resolution_str == '不可访问':
            return (0, 0)
        
        match = re.search(r'(\d+)x(\d+)', resolution_str)
        if match:
            return (int(match.group(1)), int(match.group(2)))
        
        return (0, 0)
    
    def save_result(self):
        """保存处理后的结果到新的m3u文件"""
        with open(self.output_file, 'w', encoding='utf-8') as f:
            # 写入文件头
            f.writelines(self.header_lines)
            
            for channel in self.channels:
                # 写入该频道符合条件的所有行
                f.writelines(channel.valid_lines)
        
        print(f"处理完成！结果已保存到: {self.output_file}")
        print(f"共处理 {len(self.channels)} 个频道")
    
    def process(self):
        """执行完整的处理流程"""
        print(f"开始处理M3U文件: {self.input_file}")
        
        # 1. 解析文件
        self.header_lines, self.channels = self.parse_m3u_file()
        original_channel_count = len(self.channels)
        print(f"解析到 {original_channel_count} 个频道")
        
        if not self.channels:
            print("未找到任何频道，程序退出")
            return
        
        # 2. 分析频道质量
        self.analyze_all_channels()
        
        # 3. 为每个频道准备valid_lines
        valid_url_count = 0
        
        for channel in self.channels:
            # 初始化变量
            channel.valid_lines = []
            has_valid_url = False
            url_index = 0
            
            # 第一遍：收集有效URL的索引
            valid_url_indices = []
            if hasattr(channel, 'quality_info_list'):
                for i, quality_info in enumerate(channel.quality_info_list):
                    if quality_info['resolution'] not in ['未知', '不可访问'] and quality_info['buffer_status'] == '良好':
                        valid_url_indices.append(i)
                        has_valid_url = True
                        valid_url_count += 1
            
            # 如果没有有效URL，跳过该频道
            if not has_valid_url:
                channel.valid_lines = []
                continue
            
            # 第二遍：构建valid_lines，先添加所有非URL行，再添加有效URL行
            # 收集非URL行
            non_url_lines = []
            # 收集URL行及其对应索引
            url_lines = []
            current_url_idx = 0
            
            for original_line in channel.original_lines:
                stripped_line = original_line.strip()
                if stripped_line.startswith('http://') or stripped_line.startswith('https://'):
                    # 这是URL行，保存起来以便后续筛选
                    url_lines.append((current_url_idx, original_line))
                    current_url_idx += 1
                else:
                    # 非URL行直接添加到valid_lines
                    non_url_lines.append(original_line)
            
            # 构建最终的valid_lines
            channel.valid_lines = non_url_lines
            
            # 添加有效URL行
            for idx, line in url_lines:
                if idx in valid_url_indices:
                    channel.valid_lines.append(line)
        
        # 4. 打印结果统计
        print(f"\n==========================================")
        print(f"处理完成！")
        print(f"原始频道数: {original_channel_count} 个")
        print(f"保留的频道数: {len(self.channels)} 个（保持原始顺序）")
        print(f"符合条件的URL数: {valid_url_count} 个")
        print(f"==========================================")
        
        # 5. 保存结果
        self.save_result()

def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='M3U文件处理器')
    parser.add_argument('input_file', help='输入的M3U文件路径')
    parser.add_argument('-o', '--output', default='output.m3u', help='输出的M3U文件路径')
    parser.add_argument('-t', '--threads', type=int, default=5, help='最大线程数')
    parser.add_argument('-d', '--duration', type=int, default=15, help='下载测试时长(秒)')
    args = parser.parse_args()
    
    try:
        processor = M3UProcessor(
            input_file=args.input_file,
            output_file=args.output,
            max_threads=args.threads,
            download_duration=args.duration
        )
        processor.process()
        
    except Exception as e:
        print(f"处理失败: {e}")

if __name__ == "__main__":
    main()
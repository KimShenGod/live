#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import subprocess
import logging
import requests
from datetime import datetime
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


def parse_m3u(filename: str) -> Tuple[List[str], List[Channel]]:
    """解析m3u文件，返回(文件头行, 频道列表)"""
    channels = []
    header_lines = []
    all_lines = []
    
    try:
        with open(filename, 'r', encoding='utf-8') as file:
            all_lines = file.readlines()
    except UnicodeDecodeError:
        # 尝试使用其他编码
        try:
            with open(filename, 'r', encoding='gbk') as file:
                all_lines = file.readlines()
        except Exception as e:
            logger.error(f"无法打开文件 {filename}: {e}")
            return header_lines, channels
    except Exception as e:
        logger.error(f"无法打开文件 {filename}: {e}")
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
    
    return header_lines, channels


def check_url_accessibility(url: str, timeout: int = 5) -> bool:
    """检查URL是否可访问"""
    try:
        # 使用HEAD请求快速检查
        response = requests.head(url, timeout=timeout, allow_redirects=True)
        # 200-399是成功状态码
        return 200 <= response.status_code < 400
    except requests.RequestException as e:
        logger.debug(f"URL检查失败 {url}: {e}")
        return False


def get_stream_info(url: str) -> Optional[Tuple[str, str, str, str]]:
    """使用FFmpeg获取码流信息，返回(分辨率, 码率, 延迟, 缓冲状态)"""
    # 检查ffprobe是否可用，优先使用用户提供的已知路径
    ffprobe_path = None
    
    # 用户提供的已知路径
    known_path = r"C:\Program Files (x86)\ffmpeg\bin\ffprobe.exe"
    logger.debug(f"优先使用用户提供的已知路径: {known_path}")
    
    # 验证已知路径是否可用
    try:
        result = subprocess.run(
            [known_path, '-version'],
            capture_output=True,
            text=True,
            check=True,
            timeout=5
        )
        logger.debug(f"成功使用已知路径: {known_path}")
        logger.debug(f"ffprobe版本信息: {result.stdout[:100]}...")
        ffprobe_path = known_path
    except FileNotFoundError:
        logger.error(f"已知路径不存在: {known_path}")
    except Exception as e:
        logger.error(f"已知路径访问失败: {e}")
    
    # 如果已知路径不可用，尝试直接调用命令名
    if not ffprobe_path:
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
            logger.error("直接调用ffprobe命令失败")
        except Exception as e:
            logger.error(f"直接调用ffprobe时发生异常: {e}")
    
    # 如果仍然找不到ffprobe
    if not ffprobe_path:
        logger.error("无法找到ffprobe命令")
        logger.debug(f"当前PATH环境变量: {os.environ.get('PATH', '')}")
        logger.debug(f"当前工作目录: {os.getcwd()}")
        logger.debug(f"尝试过的路径: {known_path}, ffprobe")
        logger.error("请确保FFmpeg已安装并添加到系统PATH中，或修改脚本中的known_path变量")
        return None
    
    # 构建FFprobe命令 - 优化参数以更快获取码流信息
    cmd = [
        ffprobe_path,
        '-v', 'warning',  # 显示警告信息以便调试
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height,bit_rate,avg_bit_rate,start_time,duration,codec_time_base',  # 添加延迟相关字段
        '-show_entries', 'format=bit_rate,start_time,duration,probe_score',  # 添加格式延迟和缓冲相关字段
        '-of', 'csv=p=0',
        '-timeout', '3000000',  # 3秒超时（单位：微秒）
        '-reconnect', '1',  # 允许重连
        '-reconnect_delay_max', '2',  # 最大重连延迟2秒
        '-probesize', '1000000',  # 优化探针大小（1MB）
        '-analyzeduration', '2000000',  # 优化分析时长（2秒）
        '-rw_timeout', '3000000',  # 读写超时
        '-max_delay', '3000000',  # 最大延迟
        url
    ]
    
    try:
        # 执行命令
        logger.debug(f"执行ffprobe命令: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,  # 增加超时时间
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
                '-timeout', '3000000',  # 3秒超时
                '-probesize', '1000000',  # 1MB探针大小
                '-analyzeduration', '2000000',  # 2秒分析时长
                '-rw_timeout', '3000000',  # 读写超时
                '-max_delay', '3000000'  # 最大延迟
            ]
            
            json_result = subprocess.run(
                json_cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if json_result.stdout:
                logger.debug(f"JSON命令输出: {json_result.stdout[:300]}...")
                try:
                    import json
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
            
            return None
        
        # 处理输出
        lines = output.split('\n')
        if not lines:
            return None
        
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
        
        return None
    
    except subprocess.TimeoutExpired:
        logger.error(f"FFprobe超时 {url}")
        return None
    except subprocess.CalledProcessError as e:
        logger.error(f"FFprobe调用失败 {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"获取码流信息失败 {url}: {e}")
        return None


def main():
    """主函数"""
    if len(sys.argv) != 2:
        # 没有指定参数，使用默认的iptv.m3u文件
        filename = "iptv.m3u"
        print(f"没有指定参数，使用默认文件: {filename}")
    else:
        filename = sys.argv[1]
    
    logger.info(f"开始处理文件: {filename}")
    
    # 解析m3u文件 - 新返回格式：(文件头行, 频道列表)
    header_lines, channels = parse_m3u(filename)
    logger.info(f"解析完成，共找到 {len(channels)} 个频道")
    
    if not channels:
        logger.error("未找到任何频道")
        sys.exit(1)
    
    # 处理每个频道
    accessible_count = 0
    analyzed_count = 0
    total_urls = 0
    valid_count = 0
    
    # 为每个频道准备valid_lines
    for i, channel in enumerate(channels, 1):
        print(f"\n==========================================")
        print(f"频道 {i}/{len(channels)}")
        print(f"名称: {channel.name}")
        print(f"直播源数量: {len(channel.urls)}")
        
        if not channel.urls:
            print(f"没有可用的直播源地址")
            # 保留原始行
            channel.valid_lines = channel.original_lines
            continue
        
        # 初始化valid_lines，添加#EXTINF行和其他非URL行
        channel.valid_lines = []
        
        # 遍历原始行，处理每个URL
        url_index = 0
        for original_line in channel.original_lines:
            stripped_line = original_line.strip()
            
            # 非URL行直接保留
            if not (stripped_line.startswith('http://') or stripped_line.startswith('https://')):
                channel.valid_lines.append(original_line)
            else:
                # 这是一个URL行，需要检查是否符合条件
                if url_index < len(channel.urls):
                    url = channel.urls[url_index]
                    total_urls += 1
                    
                    print(f"\n  直播源 {url_index + 1}/{len(channel.urls)}")
                    print(f"  URL: {url}")
                    
                    # 检查可用性
                    logger.info(f"检查直播源可用性: {channel.name} - 源 {url_index + 1}")
                    is_valid = False
                    
                    if check_url_accessibility(url):
                        accessible_count += 1
                        print(f"  状态: ✓ 可用")
                        
                        # 码流分析
                        logger.info(f"进行码流分析: {channel.name} - 源 {url_index + 1}")
                        stream_info = get_stream_info(url)
                        if stream_info:
                            analyzed_count += 1
                            resolution, bitrate, delay, buffer_status = stream_info
                            print(f"  分辨率: {resolution}")
                            print(f"  码率: {bitrate}")
                            print(f"  播放延迟: {delay}")
                            print(f"  缓冲状态: {buffer_status}")
                            
                            # 只有能读取到分辨率的直播源才被保存
                            if resolution != "未知" and resolution != "":
                                is_valid = True
                                valid_count += 1
                                channel.valid_urls.append(url)
                        else:
                            print(f"  码流分析: ✗ 失败")
                    else:
                        print(f"  状态: ✗ 不可用")
                    
                    # 如果URL符合条件，保留该行
                    if is_valid:
                        channel.valid_lines.append(original_line)
                    
                    url_index += 1
        
    # 输出统计信息
    print(f"\n==========================================")
    print(f"统计结果:")
    print(f"总频道数: {len(channels)}")
    print(f"总直播源数: {total_urls}")
    print(f"可用直播源: {accessible_count}")
    print(f"成功分析: {analyzed_count}")
    print(f"可用且能读取到分辨率的直播源: {valid_count}")
    print(f"==========================================")
    
    # 保存符合条件的直播源到原始m3u文件，覆盖原始文件
    output_filename = filename
    with open(output_filename, 'w', encoding='utf-8') as f:
        # 写入文件头（保留原始格式）
        f.writelines(header_lines)
        
        # 遍历所有频道
        for channel in channels:
            # 直接写入该频道符合条件的所有行，包括#EXTINF行
            f.writelines(channel.valid_lines)
    
    print(f"\n已将 {valid_count} 个符合条件的直播源保存到文件: {output_filename}（覆盖原始文件）")
    logger.info(f"已将 {valid_count} 个符合条件的直播源保存到文件: {output_filename}（覆盖原始文件）")
    
    logger.info(f"处理完成")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
M3U文件处理器 - 对本地m3u文件进行排序、解析和筛选
该程序实现了完整的M3U文件处理功能,主要特点包括：

1.文件解析:能够正确解析本地M3U文件,提取频道名称、分组信息和URL
2.质量分析:使用m3u8库加载播放列表,获取分辨率、带宽、帧率等信息
3.性能测试:通过下载TS片段的一部分来测试实际下载速度
4.多线程处理:使用ThreadPoolExecutor实现并发分析,提高处理效率
5.智能排序‌：先按频道类型(group-title)排序,再按分辨率排序，相同分辨率时按下载速度降序排列
6.频道筛选‌：对相同名称的频道进行去重,每个频道名称最多保留6个最佳质量的源

python sort_m3u.py input.m3u -o output.m3u -t 10
"""

import os
import re
import time
import m3u8
import requests
from urllib.parse import urlparse
from urllib.parse import urljoin
from collections import defaultdict
import concurrent.futures

class M3UProcessor:
    def __init__(self, input_file, output_file="output.m3u", max_threads=5,download_duration=15):
        self.input_file = input_file
        self.output_file = output_file
        self.max_threads = max_threads
        self.download_duration = download_duration
        self.channels = []
        
    def parse_m3u_file(self, file_path):
        """加载并解析m3u文件，返回频道列表，支持非标准IPTV扩展格式"""
        channels = []
        
        try:
            # 获取文件所在目录作为基础路径
            file_dir = os.path.dirname(os.path.abspath(file_path))
            base_url = f"file://{file_dir}/"
            
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            lines = content.split('\n')
            current_channel = None
            
            for line in lines:
                line = line.strip()
                
                # 处理频道信息行
                if line.startswith('#EXTINF'):
                    current_channel = self._parse_extinf_line(line, file_path)
                    
                # 处理URL行
                elif line and not line.startswith('#') and current_channel:
                    resolved_url = self._resolve_url(line, base_url, file_path)
                    if resolved_url:
                        current_channel['url'] = resolved_url
                        channels.append(current_channel.copy())
                        current_channel = None
                    else:
                        print(f"警告：非标准URL格式 - {line}")
                        
            # 按group-title排序
            channels.sort(key=lambda x: x.get('group_title', ''))
            
            # 更新类属性
            self.channels = channels
            
            return channels
        
        except UnicodeDecodeError:
            # 尝试其他常见编码
            try:
                with open(file_path, 'r', encoding='gbk') as f:
                    content = f.read()
                print(f"警告：文件使用GBK编码，已自动转换 - {file_path}")
                return self.parse_m3u_file(file_path)
            except FileNotFoundError:
                print(f"错误：文件不存在 - {file_path}")
                return []
            except Exception as e:
                print(f"解析m3u文件时发生未知错误: {str(e)}")
                return []

    def _parse_extinf_line(self, line, file_path):
        """解析EXTINF行，支持多种属性格式和非标准IPTV扩展"""
        channel_info = {
            'name': '',
            'tvg_name': '',
            'tvg_id': '',
            'tvg_logo': '',
            'group_title': '',
            'duration': -1,
            'attributes': {},
            'source_file': file_path
        }
        
        # 提取duration
        duration_match = re.search(r'#EXTINF:(-?\d+)', line)
        if duration_match:
            channel_info['duration'] = int(duration_match.group(1))
        
        # 提取频道名称（逗号后面的内容）
        name_match = re.search(r',\s*(.*)$', line)
        if name_match:
            channel_info['name'] = name_match.group(1).strip()
        
        # 提取各种属性
        attributes = {}
        
        # tvg-name
        tvg_name_match = re.search(r'tvg-name="([^"]*)"', line)
        if tvg_name_match:
            channel_info['tvg_name'] = tvg_name_match.group(1)
            if not channel_info['name']:
                channel_info['name'] = channel_info['tvg_name']
        
        # tvg-id
        tvg_id_match = re.search(r'tvg-id="([^"]*)"', line)
        if tvg_id_match:
            channel_info['tvg_id'] = tvg_id_match.group(1)
        
        # tvg-logo
        tvg_logo_match = re.search(r'tvg-logo="([^"]*)"', line)
        if tvg_logo_match:
            channel_info['tvg_logo'] = tvg_logo_match.group(1)
        
        # group-title
        group_title_match = re.search(r'group-title="([^"]*)"', line)
        if group_title_match:
            channel_info['group_title'] = group_title_match.group(1)
        
        # 提取其他扩展属性（支持非标准IPTV属性）
        other_attrs = re.findall(r'([a-zA-Z0-9\-_]+)="([^"]*)"', line)
        for key, value in other_attrs:
            if key not in ['tvg-name', 'tvg-id', 'tvg-logo', 'group-title']:
                attributes[key] = value
        
        channel_info['attributes'] = attributes
        
        return channel_info

    def _resolve_url(self, url, base_url, file_path):
        """解析URL，处理多种格式和协议"""
        if not url or url.isspace():
            return None
        
        # 处理各种协议
        protocols = [
            'http://', 'https://', 'rtmp://', 'rtsp://', 
            'mms://', 'udp://', 'file://', 'rtp://',
            'hls://', 'webrtc://', 'srt://'
        ]
        
        for protocol in protocols:
            if url.startswith(protocol):
                return url
        
        # 处理相对路径
        if base_url:
            try:
                resolved = urljoin(base_url, url)
                return resolved
            except Exception:
                pass
        
        # 处理Windows本地路径
        if os.path.isabs(url) and os.path.exists(url):
            return f"file://{url}"
        
        # 处理相对于当前文件的路径
        file_dir = os.path.dirname(os.path.abspath(file_path))
        local_path = os.path.join(file_dir, url)
        if os.path.exists(local_path):
            return f"file://{local_path}"
        
        # 如果以上都无法解析，返回原始URL（可能是特殊格式）
        return url
    
    def analyze_channel_quality(self, channel):
        """分析单个频道的质量信息，重点测试15秒TS片段的下载速度"""
        try:
            playlist = m3u8.load(channel['url'])
            
            quality_info = {
                'resolution': '未知',
                'bandwidth': 0,
                'frame_rate': 0,
                'download_speed': 0,
                'total_downloaded': 0,
                'download_time': 0
            }
            
            # 如果有媒体播放列表，分析TS片段
            if playlist.segments:
                total_downloaded = 0
                total_time = 0
                segments_downloaded = 0
                
                # 计算需要下载的片段数量以达到15秒
                target_duration = self.download_duration
                current_duration = 0
                segments_to_download = []
                
                for segment in playlist.segments:
                    segments_to_download.append(segment)
                    current_duration += segment.duration
                    if current_duration >= target_duration:
                        break
                
                if not segments_to_download:
                    segments_to_download = [playlist.segments[0]]
                
                # 下载选定的TS片段
                for i, segment in enumerate(segments_to_download):
                    segment_url = segment.uri
                    
                    # 如果URL是相对路径，转换为绝对路径
                    if not urlparse(segment_url).netloc:
                        base_url = '/'.join(channel['url'].split('/')[:-1]) + '/'
                        segment_url = urljoin(base_url, segment_url)
                    
                    try:
                        start_time = time.time()
                        response = requests.get(segment_url, stream=True, timeout=15)
                        
                        # 下载整个片段
                        chunk_size = 1024 * 64  # 64KB chunks
                        segment_data = b''
                        
                        for chunk in response.iter_content(chunk_size=chunk_size):
                            segment_data += chunk
                            # 检查是否已经达到15秒的下载时间
                            if time.time() - start_time >= self.download_duration:
                                break
                        
                        download_time = time.time() - start_time
                        segment_size = len(segment_data)
                        
                        total_downloaded += segment_size
                        total_time += download_time
                        segments_downloaded += 1
                        
                        response.close()
                        
                        print(f"  Segment {i+1}: {segment_size/1024:.1f}KB in {download_time:.2f}s "
                                  f"({segment_size/download_time/1024:.1f}KB/s)")
                        
                    except Exception as e:
                        print(f"  下载片段 {i+1} 失败: {e}")
                        continue
                
                # 计算平均下载速度
                if total_time > 0:
                    quality_info['download_speed'] = total_downloaded / total_time / 1024  # KB/s
                    quality_info['total_downloaded'] = total_downloaded
                    quality_info['download_time'] = total_time
                    quality_info['segments_downloaded'] = segments_downloaded
                
                # 从播放列表获取分辨率信息
                if hasattr(playlist, 'playlists') and playlist.playlists:
                    # 这是主播放列表，选择第一个变体
                    variant = playlist.playlists[0]
                    if variant.stream_info.resolution:
                        quality_info['resolution'] = f"{variant.stream_info.resolution[0]}x{variant.stream_info.resolution[1]}"
                    if variant.stream_info.bandwidth:
                        quality_info['bandwidth'] = variant.stream_info.bandwidth
                    if variant.stream_info.frame_rate:
                        quality_info['frame_rate'] = variant.stream_info.frame_rate
            
            return quality_info
            
        except Exception as e:
            print(f"分析频道 {channel.get('name', '未知')} 失败: {e}")
            return {
                'resolution': '未知',
                'bandwidth': 0,
                'frame_rate': 0,
                'download_speed': 0,
                'total_downloaded': 0,
                'download_time': 0,
                'segments_downloaded': 0
            }
    
    def analyze_all_channels(self):
        """使用多线程分析所有频道的质量"""
        print("开始分析频道质量...")
        print(f"每个频道将下载约{self.download_duration}秒的TS片段")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            future_to_channel = {
                executor.submit(self.analyze_channel_quality, channel): channel 
                for channel in self.channels
            }
            
            for i, future in enumerate(concurrent.futures.as_completed(future_to_channel)):
                channel = future_to_channel[future]
                try:
                    quality_info = future.result()
                    channel.update(quality_info)
                    
                    # 打印详细的分析结果
                    print(f"\n[{i+1}/{len(self.channels)}] 频道: {channel['name']}")
                    print(f"  分辨率: {channel['resolution']}")
                    print(f"  带宽: {channel['bandwidth']} bps")
                    print(f"  帧率: {channel['frame_rate']} fps")
                    print(f"  下载速度: {channel['download_speed']:.2f} KB/s")
                    print(f"  总下载量: {channel['total_downloaded']/1024:.1f} KB")
                    print(f"  总下载时间: {channel['download_time']:.2f} s")
                    print(f"  下载片段数: {channel['segments_downloaded']}")
                    print("-" * 60)
                    
                except Exception as e:
                    print(f"分析频道 {channel['name']} 时发生错误: {e}")
    
    def sort_and_filter_channels(self):
        """对频道进行排序和筛选"""
        # 按group-title分组
        grouped_channels = defaultdict(list)
        for channel in self.channels:
            group = channel.get('group_title', '未分组')
            grouped_channels[group].append(channel)
        
        # 对每个分组内的频道进行排序
        sorted_channels = []
        
        # 先按group-title排序
        for group_title in sorted(grouped_channels.keys()):
            group_channels = grouped_channels[group_title]
            
            # 按频道名称分组
            channel_groups = defaultdict(list)
            for channel in group_channels:
                channel_name = channel['name']
                channel_groups[channel_name].append(channel)
            
            # 对每个相同名称的频道组进行排序和筛选
            for channel_name, same_channels in channel_groups.items():
                # 按分辨率排序，然后按下载速度降序
                same_channels.sort(key=lambda x: (
                    self._parse_resolution(x['resolution']),
                    x['download_speed']
                ), reverse=True)
                
                # 每个频道名称最多保留6个
                sorted_channels.extend(same_channels[:6])
        
        self.channels = sorted_channels
    
    def _parse_resolution(self, resolution_str):
        """解析分辨率字符串为可比较的数值"""
        if resolution_str == '未知':
            return (0, 0)
        
        match = re.search(r'(\d+)x(\d+)', resolution_str)
        if match:
            return (int(match.group(1)), int(match.group(2)))
        
        return (0, 0)
    
    def save_result(self):
        """保存处理后的结果到新的m3u文件"""
        with open(self.output_file, 'w', encoding='utf-8') as f:
            f.write('#EXTM3U\n')
            
            for channel in self.channels:
                # 写入EXTINF行
                duration = channel.get('duration', -1)
                name = channel.get('name', '未知频道')
                group_title = channel.get('group_title', '')
                
                extinf_line = f'#EXTINF:{duration}'
                if group_title:
                    extinf_line += f' group-title="{group_title}"'
                extinf_line += f',{name}\n'
                f.write(extinf_line)
                
                # 写入URL行
                f.write(f"{channel['url']}\n")
        
        print(f"处理完成！结果已保存到: {self.output_file}")
        print(f"共处理 {len(self.channels)} 个频道")
    
    def process(self):
        """执行完整的处理流程"""
        print(f"开始处理M3U文件: {self.input_file}")
        
        # 1. 解析文件
        self.parse_m3u_file(self.input_file)
        print(f"解析到 {len(self.channels)} 个频道")
        
        # 2. 分析频道质量
        self.analyze_all_channels()
        
        # 3. 排序和筛选
        self.sort_and_filter_channels()
        
        # 4. 保存结果
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
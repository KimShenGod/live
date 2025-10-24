#!/usr/bin/env python3
"""
M3U文件处理器 - 合并多个M3U文件中的频道信息
支持非标准IPTV扩展格式，检测TS片段可访问性，并按分辨率排序
"""
import re
import requests
import time
import concurrent.futures
from urllib.parse import urljoin, urlparse
from collections import defaultdict
import m3u8

class M3UProcessor:
    def __init__(self, timeout=10, max_workers=10):
        self.timeout = timeout
        self.max_workers = max_workers
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def load_m3u_url(self, url):
        """加载M3U文件并解析频道信息"""
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.encoding = 'utf-8'
            content = response.text
            return self.parse_m3u_content(content, url)
        except Exception as e:
            print(f"加载M3U文件失败 {url}: {e}")
            return []
    
    def parse_m3u_content(self, content, base_url):
        """解析M3U文件内容"""
        channels = []
        lines = content.split('\n')
        i = 0
        
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('#EXTINF:'):
                # 解析EXTINF行
                channel_info = self.parse_extinf_line(line)
                i += 1
                if i < len(lines):
                    stream_url = lines[i].strip()
                    if stream_url and not stream_url.startswith('#'):
                        channel_info['url'] = self.resolve_url(stream_url, base_url)
                        channels.append(channel_info)
            i += 1
        
        return channels
    
    def parse_extinf_line(self, line):
        """解析EXTINF行，支持多种属性格式"""
        channel_info = {
            'tvg-name': '',
            'tvg-id': '',
            'tvg-logo': '',
            'group-title': '',
            'name': '',
            'duration': -1,
            'attributes': {}
        }
        
        # 提取duration和name
        duration_match = re.search(r'#EXTINF:(-?\d+)(?:\s+(.*))?', line)
        if duration_match:
            channel_info['duration'] = int(duration_match.group(1))
            if duration_match.group(2):
                channel_info['name'] = duration_match.group(2).strip()
        
        # 提取各种属性
        attributes = {}
        
        # tvg-name
        tvg_name_match = re.search(r'tvg-name="([^"]*)"', line)
        if tvg_name_match:
            channel_info['tvg-name'] = tvg_name_match.group(1)
            if not channel_info['name']:
                channel_info['name'] = channel_info['tvg-name']
        
        # tvg-id
        tvg_id_match = re.search(r'tvg-id="([^"]*)"', line)
        if tvg_id_match:
            channel_info['tvg-id'] = tvg_id_match.group(1)
        
        # tvg-logo
        tvg_logo_match = re.search(r'tvg-logo="([^"]*)"', line)
        if tvg_logo_match:
            channel_info['tvg-logo'] = tvg_logo_match.group(1)
        
        # group-title
        group_title_match = re.search(r'group-title="([^"]*)"', line)
        if group_title_match:
            channel_info['group-title'] = group_title_match.group(1)
        
        # 提取其他属性
        other_attrs = re.findall(r'([a-zA-Z-]+)="([^"]*)"', line)
        for key, value in other_attrs:
            if key not in ['tvg-name', 'tvg-id', 'tvg-logo', 'group-title']:
                attributes[key] = value
        
        channel_info['attributes'] = attributes
        
        return channel_info
    
    def resolve_url(self, url, base_url):
        """解析URL，处理相对路径"""
        if url.startswith('http://') or url.startswith('https://'):
            return url
        return urljoin(base_url, url)
    
    def detect_stream_info(self, channel):
        """检测流信息和可访问性"""
        try:
            start_time = time.time()
            
            if channel['url'].endswith('.m3u8'):
                # 处理m3u8流
                playlist = m3u8.load(channel['url'], timeout=self.timeout)
                
                if playlist.segments:
                    # 测试第一个TS片段
                    ts_url = self.resolve_url(playlist.segments[0].uri, channel['url'])
                    ts_response = self.session.head(ts_url, timeout=self.timeout)
                    
                    if ts_response.status_code == 200:
                        download_time = time.time() - start_time
                        resolution = self.extract_resolution(playlist)
                        
                        return {
                            'accessible': True,
                            'resolution': resolution,
                            'download_time': download_time,
                            'stream_type': 'm3u8'
                        }
            else:
                # 直接测试URL
                response = self.session.head(channel['url'], timeout=self.timeout)
                if response.status_code == 200:
                    download_time = time.time() - start_time
                    
                    return {
                        'accessible': True,
                        'resolution': 'unknown',
                        'download_time': download_time,
                        'stream_type': 'direct'
                        }
        
        except Exception as e:
            print(f"检测流信息失败 {channel.get('name', 'unknown')}: {e}")
        
        return {
            'accessible': False,
            'resolution': 'unknown',
            'download_time': float('inf'),
            'stream_type': 'unknown'
        }
    
    def extract_resolution(self, playlist):
        """从m3u8播放列表中提取分辨率"""
        try:
            if hasattr(playlist, 'playlists') and playlist.playlists:
                # 多码流情况
                resolutions = []
                for variant in playlist.playlists:
                    if variant.stream_info.resolution:
                        width = variant.stream_info.resolution[0]
                        height = variant.stream_info.resolution[1]
                        resolutions.append(f"{width}x{height}")
                
                if resolutions:
                    return ', '.join(resolutions)
            
            # 单码流或无法获取分辨率
            return 'unknown'
            
        except Exception:
            return 'unknown'
    
    def process_channels(self, m3u_urls):
        """处理所有M3U文件中的频道"""
        all_channels = []
        
        print(f"开始处理 {len(m3u_urls)} 个M3U文件...")
        
        # 并行加载所有M3U文件
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_url = {executor.submit(self.load_m3u_url, url): url for url in m3u_urls}
            
            for future in concurrent.futures.as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    channels = future.result()
                    all_channels.extend(channels)
                    print(f"成功加载 {url}: {len(channels)} 个频道")
                except Exception as e:
                    print(f"处理M3U文件失败 {url}: {e}")
        
        print(f"总共加载 {len(all_channels)} 个频道")
        
        # 检测频道可访问性
        accessible_channels = []
        print("开始检测频道可访问性...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_channel = {executor.submit(self.detect_stream_info, channel): channel for channel in all_channels}
            
            for future in concurrent.futures.as_completed(future_to_channel):
                channel = future_to_channel[future]
                try:
                    stream_info = future.result()
                    if stream_info['accessible']:
                        channel.update(stream_info)
                        accessible_channels.append(channel)
                except Exception as e:
                    print(f"可访问性处理失败 {url}: {e}")
            
        print(f"可访问频道: {len(accessible_channels)} 个")
        return accessible_channels
    
    def merge_channels(self, channels):
        """合并相同频道名的频道"""
        channel_groups = defaultdict(list)
        
        for channel in channels:
            channel_name = channel.get('tvg-name') or channel.get('name', '')
            if channel_name:
                channel_groups[channel_name].append(channel)
        
        return channel_groups
    
    def sort_and_filter_channels(self, channel_groups):
        """排序和过滤频道"""
        sorted_channels = []
        
        for channel_name, channel_list in channel_groups.items():
            # 按分辨率排序，然后按下载时间排序
            def sort_key(channel):
                resolution = channel.get('resolution', 'unknown')
                download_time = channel.get('download_time', float('inf'))
                return (resolution, -download_time if download_time > 10 else 0)
            
            sorted_list = sorted(channel_list, key=sort_key, reverse=True)
            # 每个频道最多保留10个
            sorted_channels.extend(sorted_list[:10])
        
        return sorted_channels
    
    def generate_m3u_content(self, channels):
        """生成最终的M3U文件内容"""
        lines = ['#EXTM3U']
        
        for channel in channels:
            # 构建EXTINF行
            extinf_parts = [f"#EXTINF:{channel.get('duration', -1)}"]
            
            # 添加标准属性
            if channel.get('tvg-id'):
                extinf_parts.append(f'tvg-id="{channel["tvg-id"]}"')
            if channel.get('tvg-name'):
                extinf_parts.append(f'tvg-name="{channel["tvg-name"]}"')
            if channel.get('tvg-logo'):
                extinf_parts.append(f'tvg-logo="{channel["tvg-logo"]}"')
            if channel.get('group-title'):
                extinf_parts.append(f'group-title="{channel["group-title"]}"')
            
            # 添加其他属性
            for key, value in channel.get('attributes', {}).items():
                extinf_parts.append(f'{key}="{value}"')
            
            # 添加频道名称
            extinf_parts.append(channel.get('name', ''))
            
            lines.append(' '.join(extinf_parts))
            lines.append(channel['url'])
        
        return '\n'.join(lines)
    
    def save_m3u_file(self, content, filename):
        """保存M3U文件"""
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"M3U文件已保存: {filename}")
    
    def process(self, m3u_urls, output_file='online_merged_channels.m3u'):
        """主处理流程"""
        # 处理所有频道
        channels = self.process_channels(m3u_urls)
        
        # 按频道名合并
        channel_groups = self.merge_channels(channels)
        
        # 排序和过滤
        final_channels = self.sort_and_filter_channels(channel_groups)
        
        # 生成M3U内容
        m3u_content = self.generate_m3u_content(final_channels)
        
        # 保存文件
        self.save_m3u_file(m3u_content, output_file)
        
        return {
            'total_channels': len(channels),
            'accessible_channels': len(final_channels),
            'unique_channel_names': len(channel_groups),
            'output_file': output_file
        }


def main():
    processor = M3UProcessor()
    
    # 示例M3U URL列表
    m3u_urls = [
        "https://raw.githubusercontent.com/kimwang1978/collect-txt/refs/heads/main/bbxx.m3u",
        #"https://raw.githubusercontent.com/KimShenGod/live/main/cqyx.m3u8",
        # 添加更多M3U URL...
    ]
    
    processor = M3UProcessor(timeout=10, max_workers=5)
    
    print("M3U文件处理器启动...")
    result = processor.process(m3u_urls)
    
    print(f"\n处理完成!")
    print(f"总频道数: {result['total_channels']}")
    print(f"可访问频道: {result['accessible_channels']}")
    print(f"唯一频道名: {result['unique_channel_names']}")
    print(f"输出文件: {result['output_file']}")

if __name__ == "__main__":
    main()



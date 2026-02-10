#!/usr/bin/env python3
"""
M3U频道合并工具
功能：
1. 合并相同频道的直播源地址到同一个频道下
2. 只保留一条最完整的#EXTINF行信息
3. 支持多种URL协议
4. 完善的错误处理
"""

import os
import re
from typing import Dict, List, Tuple, Optional


def parse_extinf_line(line: str) -> Tuple[str, Dict[str, str]]:
    """
    解析#EXTINF行，返回频道名称和属性字典
    
    Args:
        line: #EXTINF行内容
        
    Returns:
        tuple: (频道名称, 属性字典)
    """
    attributes = {}
    
    # 提取持续时间
    duration_match = re.search(r'#EXTINF:(-?\d+)', line)
    if duration_match:
        attributes['duration'] = duration_match.group(1)
    
    # 提取频道名称（逗号后面的内容）
    name_match = re.search(r',\s*(.*)$', line)
    channel_name = name_match.group(1).strip() if name_match else "未知频道"
    
    # 提取所有引号包裹的属性
    attrs = re.findall(r'(\w[\w-]*)="([^"]*)"', line)
    for key, value in attrs:
        attributes[key] = value
    
    return channel_name, attributes


def is_valid_url(line: str) -> bool:
    """
    判断是否为有效的URL行
    
    Args:
        line: 待检查的行
        
    Returns:
        bool: 是否为有效的URL
    """
    stripped = line.strip()
    return stripped.startswith((
        'http://', 'https://', 'rtmp://', 'rtsp://',
        'mms://', 'udp://', 'rtp://', 'srt://'
    ))


def calculate_extinf_completeness(extinf_line: str) -> int:
    """
    计算#EXTINF行的完整性分数
    分数越高，表示#EXTINF行包含的信息越完整
    
    Args:
        extinf_line: #EXTINF行内容
        
    Returns:
        int: 完整性分数
    """
    # 基础分数：1分（至少包含频道名称）
    score = 1
    
    # 持续时间：+1分
    if re.search(r'#EXTINF:(-?\d+)', extinf_line):
        score += 1
    
    # 每个属性：+1分
    score += len(re.findall(r'\w[\w-]*="[^"]*"', extinf_line))
    
    return score


def process_m3u_file(input_path: str, output_path: str) -> None:
    """
    处理M3U文件，合并相同频道的直播源
    
    Args:
        input_path: 输入M3U文件路径
        output_path: 输出M3U文件路径
    """
    print(f"开始处理文件: {input_path}")
    
    # 读取输入文件
    with open(input_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # 解析文件，按频道分组
    channel_groups: Dict[str, Dict] = {}
    current_extinf: Optional[str] = None
    current_channel: Optional[str] = None
    current_attrs: Dict[str, str] = {}
    
    line_count = len(lines)
    print(f"文件总行数: {line_count}")
    
    for i, line in enumerate(lines):
        line = line.rstrip('\n')
        stripped = line.strip()
        
        if stripped.startswith('#EXTM3U'):
            # M3U头，跳过
            continue
            
        elif stripped.startswith('#EXTINF'):
            # 处理#EXTINF行
            current_channel, current_attrs = parse_extinf_line(line)
            current_extinf = line
            
        elif is_valid_url(line) and current_extinf and current_channel:
            # 处理URL行，关联到当前频道
            if current_channel not in channel_groups:
                # 新频道，初始化
                channel_groups[current_channel] = {
                    'extinf': current_extinf,
                    'completeness_score': calculate_extinf_completeness(current_extinf),
                    'urls': [line],
                    'original_extinf': current_extinf,
                    'group_title': current_attrs.get('group-title', '')
                }
            else:
                # 已存在的频道
                existing = channel_groups[current_channel]
                current_score = calculate_extinf_completeness(current_extinf)
                
                # 如果当前#EXTINF更完整，替换旧的
                if current_score > existing['completeness_score']:
                    existing['extinf'] = current_extinf
                    existing['completeness_score'] = current_score
                    existing['group_title'] = current_attrs.get('group-title', '')
                
                # 添加URL到列表
                existing['urls'].append(line)
            
            # 重置当前状态
            current_extinf = None
            current_channel = None
            current_attrs = {}
    
    # 1. 删除不需要的频道
    # 要删除的group-title列表
    groups_to_delete = ['更新时间', '体育赛事', '🏈体育赛事🏆️', '直播中国']
    filtered_channels = {}
    
    for channel_name, group in channel_groups.items():
        extinf_line = group['extinf']
        group_title = group['group_title']
        
        if group_title not in groups_to_delete:
            # 2. 合并类别并更新extinf行
            new_group_title = group_title
            
            # 港澳台和💓港澳台📶合并为💓港澳台📶
            if group_title == '港澳台':
                new_group_title = '💓港澳台📶'
            # 💓专享央视和🌐央视频道合并为🌐央视频道
            elif group_title == '💓专享央视':
                new_group_title = '🌐央视频道'
            # 💓专享卫视和📡卫视频道合并为📡卫视频道
            elif group_title == '💓专享卫视':
                new_group_title = '📡卫视频道'
            
            # 如果group-title发生了变化，更新extinf行
            if new_group_title != group_title:
                # 更新extinf行中的group-title
                updated_extinf = re.sub(
                    r'group-title="[^"]*"',
                    f'group-title="{new_group_title}"',
                    extinf_line
                )
                group['extinf'] = updated_extinf
                group['group_title'] = new_group_title
            
            filtered_channels[channel_name] = group
    
    print(f"删除指定group-title后的频道数量: {len(filtered_channels)}")
    
    # 3. 定义group-title的优先级顺序
    group_priority = {
        '🌐央视频道': 1,       # 央视合并后保留
        '📡卫视频道': 2,       # 卫视合并后保留
        '💓港澳台📶': 3,       # 港澳台合并后保留
        '💓台湾台📶': 4,        # 台湾台
        '电影频道': 5,         # 电影频道
        'MTV': 6,              # MTV
        '专项源': 7,           # 专项源
        '定制台': 8,           # 定制台
        '儿童专享': 9,         # 儿童专享
        '其他': 10             # 其他
    }
    
    # 3. 定义排序键函数
    def channel_sort_key(channel_item):
        channel_name, group = channel_item
        
        # 获取group-title
        group_title = group['group_title']
        
        # 获取group-title的优先级
        group_rank = group_priority.get(group_title, 10)  # 默认最低优先级
        
        # 首先按group-title优先级排序，然后按group-title名称排序，确保相同group-title的频道在一起
        # 然后处理CCTV频道的特殊排序
        if channel_name.lower().startswith('cctv'):
            # 提取CCTV数字，例如"CCTV1" → 1，"CCTV-10" → 10
            cctv_match = re.search(r'cctv[-]?([0-9]+)', channel_name.lower())
            if cctv_match:
                cctv_num = int(cctv_match.group(1))
                return (group_rank, group_title, 'cctv', cctv_num, channel_name)
        
        # 非CCTV频道，按名称排序
        return (group_rank, group_title, 'other', channel_name)
    
    # 4. 按排序键对频道进行排序
    sorted_channels = sorted(filtered_channels.items(), key=channel_sort_key)
    
    # 5. 写入输出文件
    with open(output_path, 'w', encoding='utf-8') as f:
        # 写入M3U头
        f.write("#EXTM3U\n")
        
        # 按排序顺序输出频道
        for channel_name, group in sorted_channels:
            # 写入最完整的#EXTINF行
            f.write(f"{group['extinf']}\n")
            
            # 写入该频道的所有URL
            for url in group['urls']:
                f.write(f"{url}\n")
    
    # 统计信息
    original_channel_count = sum(1 for line in lines if line.strip().startswith('#EXTINF'))
    merged_channel_count = len(channel_groups)
    filtered_channel_count = len(filtered_channels)
    total_urls = sum(len(group['urls']) for group in filtered_channels.values())
    
    print(f"处理完成！")
    print(f"合并前频道数量: {original_channel_count}")
    print(f"合并后频道数量: {merged_channel_count}")
    print(f"删除指定group-title后频道数量: {filtered_channel_count}")
    print(f"总直播源数量: {total_urls}")
    print(f"结果已保存到: {output_path}")


def main():
    """主函数"""
    input_file = "bbxx_iptv.m3u"
    output_file = "bbxx_lite.m3u"
    
    # 检查输入文件是否存在
    if not os.path.exists(input_file):
        print(f"错误：输入文件 '{input_file}' 不存在！")
        print(f"请确保该文件在当前目录下，或修改脚本中的输入文件路径。")
        return
    
    # 执行处理
    process_m3u_file(input_file, output_file)


if __name__ == "__main__":
    main()

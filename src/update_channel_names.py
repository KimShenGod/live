#!/usr/bin/env python3
"""
更新M3U文件中的频道名
功能：
1. 读取bbxx_lite.m3u文件中的所有频道名
2. 与alias2.txt中的别名进行匹配
3. 将匹配到的频道名改为主名
4. 更新tvg-name字段为主名
"""

import re
import os
import zhconv

def load_alias_map(alias_file):
    """
    从alias2.txt文件加载频道别名映射
    
    Args:
        alias_file: 别名文件路径
        
    Returns:
        dict: 别名到主名的映射
    """
    alias_map = {}
    
    try:
        with open(alias_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                # 忽略注释行和空行
                if not line or line.startswith('#'):
                    continue
                
                # 分割主名和别名
                parts = line.split(',')
                main_name = parts[0]
                aliases = parts[1:] if len(parts) > 1 else []
                
                # 主名本身也是一个有效的匹配项
                alias_map[main_name] = main_name
                
                # 处理所有别名
                for alias in aliases:
                    alias_map[alias] = main_name
        
        print(f"✅ 成功加载 {len(alias_map)} 个频道别名映射")
        return alias_map
        
    except Exception as e:
        print(f"❌ 加载别名文件失败: {e}")
        return {}

def match_channel(channel_name, alias_map):
    """
    匹配频道名到主名（忽略大小写和繁简体）
    
    Args:
        channel_name: 输入的频道名
        alias_map: 别名到主名的映射
        
    Returns:
        str: 匹配到的主名，如果没有匹配返回None
    """
    # 将输入的频道名转换为简体中文并转为小写
    channel_simple_lower = zhconv.convert(channel_name, 'zh-hans').lower()
    
    # 1. 精确匹配（考虑繁简体和大小写）
    if channel_name in alias_map:
        return alias_map[channel_name]
    
    # 2. 忽略大小写和繁简体匹配
    for alias, main_name in alias_map.items():
        alias_simple_lower = zhconv.convert(alias, 'zh-hans').lower()
        if alias_simple_lower == channel_simple_lower:
            return main_name
    
    # 3. 正则表达式匹配（检查是否有以re:开头的正则表达式别名）
    for alias, main_name in alias_map.items():
        if alias.startswith('re:'):
            regex_pattern = alias[3:]
            try:
                if re.match(regex_pattern, channel_name, re.IGNORECASE):
                    return main_name
            except re.error:
                # 忽略无效的正则表达式
                continue
    
    # 4. 检查是否在别名列表中（考虑繁简体和大小写）
    # 遍历所有别名映射，检查channel_name是否是某个主名的别名
    for main_name in set(alias_map.values()):
        # 查找该主名的所有别名
        aliases = [alias for alias, name in alias_map.items() if name == main_name]
        # 检查channel_name是否在别名列表中（考虑繁简体和大小写）
        for alias in aliases:
            if zhconv.convert(alias, 'zh-hans').lower() == channel_simple_lower:
                return main_name
    
    return None

def update_m3u_channels(m3u_file, alias_map, output_file):
    """
    更新M3U文件中的频道名和tvg-name字段
    
    Args:
        m3u_file: 输入的M3U文件路径
        alias_map: 别名到主名的映射
        output_file: 输出的M3U文件路径
    """
    print(f"更新M3U文件: {m3u_file}")
    
    updated_lines = []
    current_extinf = None
    current_channel = None
    
    # 统计信息
    total_channels = 0
    matched_count = 0
    unmatched_count = 0
    unmatched_channels = []
    
    try:
        with open(m3u_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                
                # 处理#EXTINF行
                if line.startswith('#EXTINF:'):
                    current_extinf = line
                    # 提取频道名
                    channel_name = line.split(',')[-1].strip()
                    current_channel = channel_name
                    total_channels += 1
                    
                    # 匹配主名
                    matched_main = match_channel(channel_name, alias_map)
                    
                    if matched_main:
                        print(f"🔍 匹配成功: {channel_name} -> {matched_main}")
                        matched_count += 1
                        
                        # 更新频道名
                        updated_line = line.replace(channel_name, matched_main)
                        
                        # 更新tvg-name字段
                        # 检查是否已有tvg-name字段
                        if 'tvg-name=' in updated_line:
                            # 替换已有的tvg-name值
                            updated_line = re.sub(r'tvg-name="[^"]*"', f'tvg-name="{matched_main}"', updated_line)
                        else:
                            # 添加tvg-name字段
                            # 查找tvg-logo字段的位置，在其后面添加
                            if 'tvg-logo=' in updated_line:
                                updated_line = re.sub(r'(tvg-logo="[^"]*")', r'\1 tvg-name="{}"'.format(matched_main), updated_line)
                            else:
                                # 在group-title字段前添加
                                updated_line = re.sub(r'(group-title="[^"]*")', r'tvg-name="{}" \1'.format(matched_main), updated_line)
                    else:
                        updated_line = current_extinf
                        unmatched_count += 1
                        unmatched_channels.append(channel_name)
                    
                    updated_lines.append(updated_line)
                
                # 处理频道URL行
                elif current_extinf and line and not line.startswith('#'):
                    updated_lines.append(line)
                    current_extinf = None
                    current_channel = None
                
                # 处理其他行
                else:
                    updated_lines.append(line)
        
        # 写入新的M3U文件
        with open(output_file, 'w', encoding='utf-8') as f:
            for line in updated_lines:
                f.write(f"{line}\n")
        # 删除原始M3U文件
        if os.path.exists(m3u_file):
            os.remove(m3u_file)
            print(f"🗑️  已删除原始文件: {m3u_file}")
        
        # 将输出文件重命名为原始文件的名字
        if os.path.exists(output_file):
            os.rename(output_file, m3u_file)
            print(f"🔄 已将输出文件重命名为: {m3u_file}")

        # 打印统计信息
        print(f"\n📊 统计信息:")
        print(f"   总频道数: {total_channels}")
        print(f"   匹配成功数: {matched_count}")
        print(f"   匹配失败数: {unmatched_count}")
        
        if unmatched_channels:
            print(f"\n❌ 未匹配成功的频道 ({len(unmatched_channels)}个):")
            # 按字母顺序排序并打印
            for channel in sorted(unmatched_channels):
                print(f"   - {channel}")
        
        print(f"\n✅ 更新完成")
        print(f"✅ 最终结果已保存到: {m3u_file}")
        return True
        
    except Exception as e:
        print(f"❌ 更新失败: {e}")
        return False

def main():
    """
    主函数
    """
    import argparse
    
    # 创建命令行参数解析器
    parser = argparse.ArgumentParser(description='更新M3U文件中的频道名和tvg-name字段')
    
    # 添加命令行参数
    parser.add_argument('-a', '--alias', type=str, default='../alias2.txt', 
                        help='别名文件路径，默认: alias2.txt')
    parser.add_argument('-i', '--input', type=str, default='../bbxx_lite.m3u', 
                        help='输入的M3U文件路径，默认: bbxx_lite.m3u')
    parser.add_argument('-o', '--output', type=str, default='../bbxx_lite_new.m3u', 
                        help='输出的M3U文件路径，默认: bbxx_lite_new.m3u')
    
    # 解析命令行参数
    args = parser.parse_args()
    
    alias_file = args.alias
    m3u_file = args.input
    output_file = args.output
    
    # 检查文件是否存在
    if not os.path.exists(alias_file):
        print(f"❌ 别名文件 {alias_file} 不存在")
        return
    
    if not os.path.exists(m3u_file):
        print(f"❌ M3U文件 {m3u_file} 不存在")
        return
    
    # 加载别名映射
    alias_map = load_alias_map(alias_file)
    
    if not alias_map:
        print("❌ 无法加载别名映射，程序退出")
        return
    
    # 更新M3U文件中的频道名
    update_m3u_channels(m3u_file, alias_map, output_file)

if __name__ == "__main__":
    main()

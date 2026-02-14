#!/usr/bin/env python3
"""
EPG XML文件合并工具
功能：
1. 从指定URL下载EPG XML文件
2. 合并多个EPG XML文件为一个
3. 保留完整的XML结构
4. 支持错误处理和重试机制
"""

import os
import requests
from lxml import etree
from typing import List
from datetime import datetime, timedelta


def download_xml(url: str, save_path: str, timeout: int = 30, retries: int = 3) -> bool:
    """
    下载XML文件，支持重试
    
    Args:
        url: 下载URL
        save_path: 保存路径
        timeout: 超时时间（秒）
        retries: 重试次数
        
    Returns:
        bool: 下载是否成功
    """
    print(f"开始下载: {url}")
    
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()  # 检查HTTP状态码
            
            with open(save_path, 'wb') as f:
                f.write(response.content)
            
            print(f"✅ 下载成功: {url} -> {save_path}")
            return True
        except Exception as e:
            if attempt < retries:
                print(f"❌ 下载失败（尝试 {attempt+1}/{retries+1}）: {e}，将重试")
            else:
                print(f"❌ 下载最终失败: {e}")
    
    return False


def convert_utc_to_cst(time_str: str) -> str:
    """
    将UTC时间转换为UTC+8（中国标准时间）
    
    Args:
        time_str: 时间字符串，格式为 "YYYYMMDDHHMMSS +0000"
        
    Returns:
        str: 转换后的时间字符串，格式为 "YYYYMMDDHHMMSS +0800"
    """
    try:
        # 解析时间字符串
        dt = datetime.strptime(time_str[:14], "%Y%m%d%H%M%S")

        dt_cst = dt + timedelta(hours=0)
        # 格式化为字符串并添加+0800时区标识
        return dt_cst.strftime("%Y%m%d%H%M%S") + " +0800"
    except Exception as e:
        print(f"时间转换失败: {time_str}，错误: {e}")
        return time_str


def merge_xml_files(xml_files: List[str], output_file: str) -> bool:
    """
    合并多个EPG XML文件为一个，按指定顺序排列元素
    
    Args:
        xml_files: XML文件列表
        output_file: 输出文件路径
        
    Returns:
        bool: 合并是否成功
    """
    print(f"\n开始合并XML文件...")
    print(f"待合并文件: {xml_files}")
    
    try:
        # 1. 创建新的XML根节点
        root = etree.Element('tv')
        
        # 2. 按地区分类存储元素
        regions = ['CN', 'HK', 'TW']
        channel_elements = {region: [] for region in regions}
        programme_elements = {region: [] for region in regions}
        
        # 3. 遍历所有XML文件，按地区分类元素
        for xml_file in xml_files:
            print(f"处理文件: {xml_file}")
            
            # 解析XML文件
            tree = etree.parse(xml_file)
            file_root = tree.getroot()
            
            # 复制根节点的属性到新文件
            for key, value in file_root.attrib.items():
                if key not in root.attrib:
                    root.set(key, value)
            
            # 确定文件所属地区
            region = None
            if 'CN' in xml_file:
                region = 'CN'
            elif 'HK' in xml_file:
                region = 'HK'
            elif 'TW' in xml_file:
                region = 'TW'
            
            if region:
                # 分类存储元素
                for child in file_root:
                    if child.tag == 'channel':
                        channel_elements[region].append(child)
                    elif child.tag == 'programme':
                        # 转换节目开始时间和结束时间从UTC到UTC+8
                        if 'start' in child.attrib:
                            child.attrib['start'] = convert_utc_to_cst(child.attrib['start'])
                        if 'stop' in child.attrib:
                            child.attrib['stop'] = convert_utc_to_cst(child.attrib['stop'])
                        programme_elements[region].append(child)
        
        # 4. 按指定顺序添加元素到根节点
        # 先添加所有channel元素，按CN -> HK -> TW顺序
        for region in regions:
            print(f"添加{region}频道元素...")
            for channel in channel_elements[region]:
                root.append(channel)
        
        # 再添加所有programme元素，按CN -> HK -> TW顺序
        for region in regions:
            print(f"添加{region}节目元素...")
            for programme in programme_elements[region]:
                root.append(programme)
        
        # 5. 创建XML树并写入文件
        merged_tree = etree.ElementTree(root)
        merged_tree.write(
            output_file,
            encoding='utf-8',
            xml_declaration=True,
            pretty_print=True
        )
        
        print(f"✅ 合并成功，输出文件: {output_file}")
        return True
    except Exception as e:
        print(f"❌ 合并失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def extract_epg_urls(base_url: str) -> List[str]:
    """
    从指定网页提取EPG XML文件的URL
    
    Args:
        base_url: 包含EPG XML链接的网页URL
        
    Returns:
        List[str]: 提取到的EPG XML文件URL列表
    """
    print(f"从网页提取EPG XML URL: {base_url}")
    
    try:
        response = requests.get(base_url, timeout=30)
        response.raise_for_status()
        
        # 提取XML文件URL
        import re
        # 匹配所有符合格式的XML文件URL
        xml_urls = re.findall(r'https?://epg\.pw/xmltv/epg_\w+\.xml', response.text)
        
        # 去重并确保包含所需的三个URL
        unique_urls = list(set(xml_urls))
        
        # 检查是否包含CN, HK, TW的XML文件
        required_urls = []
        for url in unique_urls:
            if any(country in url for country in ['CN', 'HK', 'TW']):
                required_urls.append(url)
        
        # 如果提取到的URL不足，添加默认URL作为备选
        if len(required_urls) < 3:
            default_urls = [
                "https://epg.pw/xmltv/epg_CN.xml",
                "https://epg.pw/xmltv/epg_HK.xml",
                "https://epg.pw/xmltv/epg_TW.xml"
            ]
            
            for default_url in default_urls:
                if default_url not in required_urls:
                    required_urls.append(default_url)
        
        print(f"✅ 提取到EPG XML URL: {required_urls}")
        return required_urls
    except Exception as e:
        print(f"❌ 提取EPG XML URL失败: {e}")
        # 提取失败时返回默认URL列表
        return [
            "https://epg.pw/xmltv/epg_CN.xml",
            "https://epg.pw/xmltv/epg_HK.xml",
            "https://epg.pw/xmltv/epg_TW.xml"
        ]


def main():
    """主函数"""
    # 从网页提取EPG XML文件URL
    base_url = "https://epg.pw/xmltv.html?lang=zh-hans"
    epg_urls = extract_epg_urls(base_url)
    
    # 临时保存目录
    temp_dir = "temp_epg_xml"
    os.makedirs(temp_dir, exist_ok=True)
    
    # 下载的XML文件列表
    downloaded_files = []
    
    try:
        # 1. 下载所有XML文件
        for url in epg_urls:
            filename = os.path.basename(url)
            save_path = os.path.join(temp_dir, filename)
            
            if download_xml(url, save_path):
                downloaded_files.append(save_path)
            else:
                print(f"跳过合并 {url}，因为下载失败")
        
        # 2. 合并下载的XML文件
        if downloaded_files:
            output_file = "merged_epg.xml"
            if merge_xml_files(downloaded_files, output_file):
                print(f"\n🎉 EPG XML文件合并完成！")
                print(f"📦 合并后的文件: {output_file}")
                print(f"📄 合并的源文件数量: {len(downloaded_files)}")
            else:
                print(f"\n❌ 合并失败")
        else:
            print(f"\n❌ 没有成功下载任何XML文件，无法合并")
    finally:
        # 3. 清理临时文件
        for file in downloaded_files:
            if os.path.exists(file):
                os.remove(file)
        if os.path.exists(temp_dir):
            os.rmdir(temp_dir)
        print(f"✅ 临时文件已清理")


if __name__ == "__main__":
    main()

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
    def __init__(self, input_file, output_file="output.m3u", max_threads=5,
                 download_duration=15, channel_workers=8, fast=False, fast_sample=3,
                 max_live_duration: int | None = None, max_channels: int = 0,
                 ffprobe_timeout: int | None = None, source_timeout: int = 60):
        self.input_file = input_file
        self.output_file = output_file
        self.max_threads = max_threads
        self.download_duration = download_duration
        # 单频道内多源并行检测的并发数（与外层频道级并发相互独立）
        self.channel_workers = max(1, channel_workers)
        # CI 分块处理：最多处理前 N 个频道（0=不限制）
        self.max_channels = max(0, int(max_channels))
        # 快速模式：跳过 15s 流式稳定性测试、缩短超时与死源重试、m3u8 仅验证首分片，
        # 单源耗时从 ~25s 降到 ~7s，适合每日数万源的大规模检测；过滤效果保持不变
        # （仅放弃深度卡顿/限速指标，仍通过分片可达性 + 直链采样字节门禁剔除死源）。
        self.fast = fast
        self.fast_sample = max(1, fast_sample)
        # 固定时长源（电影/MTV/风景/广告短片）剔除阈值（秒）。真 24/7 直播流没有
        # 有限总时长（ffprobe 返回 N/A），故该阈值只用于剔除“伪直播”有限时长源。
        # 默认 None 表示使用类常量 MIN_LIVE_DURATION（3600s）。
        self.max_live_duration = max_live_duration if max_live_duration is not None \
            else self.MIN_LIVE_DURATION
        self.channels = []
        self.header_lines = []
        self._ffprobe_path = None  # 缓存 ffprobe 可执行文件路径，供轻量探测复用
        # ffprobe 子进程超时（秒）；None/0 表示沿用 fast(8s)/默认(20s) 策略
        self.ffprobe_timeout = ffprobe_timeout if ffprobe_timeout and ffprobe_timeout > 0 else None
        # 单源检测总时长上限（秒）；None 表示不限制。杜绝慢滴答源卡在非流式 GET
        # 上导致频道线程无限挂死（8 个慢源即可耗光外层频道槽位使整个 chunk 停转）
        self.source_timeout = source_timeout if source_timeout and source_timeout > 0 else None

    def _unreachable_quality(self):
        """单源检测超时/异常时的兜底质量信息，确保被 _is_url_playable 判为不可用。"""
        return {
            'resolution': '不可访问', 'bitrate': '未知', 'delay': '未知',
            'buffer_status': '未知', 'download_speed': 0, 'total_downloaded': 0,
            'download_time': 0, 'stall_events': 0, 'max_gap': 0.0,
            'verified_via_m3u8': False, 'vod_clip': False
        }

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

    def check_url_accessibility(self, url: str, timeout: int = 5, retries: int = 2) -> bool:
        """检查URL是否可访问，增加重试机制"""
        # 跳过已知无效域名
        if "iptv.catvod.com" in url:
            logger.debug(f"跳过已知无效域名: {url}")
            return False
        
        for attempt in range(retries + 1):
            try:
                # 用带 Range 的小范围 GET 代替 HEAD：很多 IPTV 服务器不响应 HEAD，
                # 或 HEAD 返回 200 但真实 GET 失败；直接取前几 KB 更能反映可播性，
                # 且能排除“连接成功但不吐数据”的死源。
                headers = {'Range': 'bytes=0-8191'}
                response = requests.get(url, headers=headers, timeout=timeout,
                                        allow_redirects=True, stream=True)
                status_ok = 200 <= response.status_code < 400
                # 读取少量数据，确认确实吐出字节
                chunk = next(response.iter_content(chunk_size=8192), b'')
                response.close()
                if status_ok and len(chunk) > 0:
                    logger.debug(f"URL检查成功 (尝试 {attempt+1}/{retries+1}) {url}: {response.status_code}, {len(chunk)}B")
                    return True
                elif attempt < retries and not status_ok:
                    logger.debug(f"URL返回非成功状态码 (尝试 {attempt+1}/{retries+1}) {url}: {response.status_code}，将重试")
                    time.sleep(0.5)
                else:
                    logger.debug(f"URL检查失败 (最终尝试) {url}: status={response.status_code}, bytes={len(chunk)}")
            except requests.RequestException as e:
                if attempt < retries:
                    logger.debug(f"URL检查失败 (尝试 {attempt+1}/{retries+1}) {url}: {e}，将重试")
                    time.sleep(0.5)
                else:
                    logger.debug(f"URL检查最终失败 {url}: {e}")
        return False

    def _segment_reachable(self, seg_url: str, timeout: int | None = None) -> bool:
        """用 HEAD/GET-Range 快速验证单个媒体分片是否真实可达。
        timeout 为 None 时按 fast 模式取较短超时，降低死源分片探测耗时。"""
        if timeout is None:
            timeout = 4 if getattr(self, 'fast', False) else 8
        try:
            resp = requests.head(seg_url, timeout=timeout, allow_redirects=True)
            if 200 <= resp.status_code < 400:
                return True
        except requests.RequestException:
            pass
        try:
            # 部分服务器不响应 HEAD，再用小范围 GET 兜底
            resp = requests.get(seg_url, headers={'Range': 'bytes=0-1023'},
                                timeout=timeout, stream=True)
            ok = 200 <= resp.status_code < 400
            resp.close()
            return ok
        except requests.RequestException:
            return False

    # 真直播不应有“总时长”；若播放列表为 VOD（含 ENDLIST）且总时长低于该阈值（秒），
    # 判定为“伪直播”——循环播放的短片/广告，予以剔除。
    # 固定时长源（电影/MTV/风景/广告短片）剔除开关。真 24/7 直播流没有有限总时长
    # （ffprobe 返回 N/A），而点播文件必然有有限总时长，因此判定规则是：
    # max_live_duration > 0 时，凡 ffprobe 探测到“有限时长”的源一律判为伪直播剔除。
    # 该值本身仅作为开关（>0 开启剔除；默认 3600 仅作占位，实际是“有限即剔除”）。
    MIN_LIVE_DURATION = 3600

    def _verify_m3u8_media(self, playlist_url: str, timeout: int | None = None):
        """解析（子）播放列表并验证前几个媒体分片是否真实可达。
        timeout 为 None 时按 fast 模式取较短超时；fast 模式下仅验证首分片，
        把单死源分片探测耗时从最坏 ~48s 降到 ~8s。
        返回 (ok, reason_or_None)；reason='vod_clip' 表示检测到伪直播短片。
        多层嵌套的主播放列表会递归下钻。"""
        if timeout is None:
            timeout = 5 if getattr(self, 'fast', False) else 8
        try:
            import m3u8
            resp = requests.get(playlist_url, timeout=timeout)
            content = None
            for enc in ['utf-8', 'gbk', 'latin-1']:
                try:
                    content = resp.content.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if not content:
                return (False, None)
            obj = m3u8.loads(content)
            segs = getattr(obj, 'segments', None)
            if not segs:
                # 可能是多层嵌套的主播放列表，递归下钻到第一个变体
                pls = getattr(obj, 'playlists', None)
                if pls:
                    return self._verify_m3u8_media(urljoin(playlist_url, pls[0].uri), timeout)
                return (False, None)
            # 识别“伪直播”：真直播列表不断滚动、没有 ENDLIST；
            # 若列表带 ENDLIST 或标记为 VOD，且分片总时长很短（如 <2 分钟），
            # 基本可断定是循环播放的广告/宣传短片，直接剔除。
            is_vod = bool(getattr(obj, 'is_endlist', False)) or \
                str(getattr(obj, 'playlist_type', '') or '').lower() == 'vod'
            if is_vod:
                total_dur = 0.0
                for s in segs:
                    try:
                        total_dur += float(getattr(s, 'duration', 0) or 0)
                    except (TypeError, ValueError):
                        pass
                # 有限时长(点播文件)即判为伪直播伪装源剔除；真直播无有限时长
                if self.max_live_duration > 0 and total_dur > 0:
                    logger.debug(
                        f"检测到伪直播短片 {playlist_url}: VOD列表，总时长{total_dur:.0f}s")
                    return (False, 'vod_clip')
            # 取前若干分片验证可达性，任一可达即视为可播放；
            # fast 模式仅查首分片，把死源分片探测耗时从最坏 ~48s 降到 ~8s
            for seg in (segs[:1] if getattr(self, 'fast', False) else segs[:3]):
                if self._segment_reachable(urljoin(playlist_url, seg.uri), timeout=timeout):
                    # 分片可达 = 是“活源”，但部分伪装源（电影/MTV/风景片）是固定时长的
                    # 有限媒体、却没有 ENDLIST/VOD 标记（伪直播滚动窗口），仅靠标志位会漏过。
                    # 用 ffprobe 探测总时长：真 24/7 直播返回 N/A(None)，不会误杀；
                    # 若返回有限时长且 < 阈值，则判定为伪直播固定时长源并剔除。
                    dur = self._probe_media_duration(playlist_url)
                    # 真 24/7 直播返回 N/A(None) 保留；有限时长(点播文件)即判为伪直播剔除
                    if self.max_live_duration > 0 and dur is not None:
                        logger.debug(
                            f"检测到伪直播固定时长源 {playlist_url}: 总时长{dur:.0f}s")
                        return (False, 'vod_clip')
                    return (True, None)
            return (False, None)
        except Exception as e:
            logger.debug(f"验证m3u8媒体失败 {playlist_url}: {e}")
            return (False, None)

    def _probe_media_duration(self, url: str, timeout: int | None = None):
        """用 ffprobe 轻量探测媒体总时长（秒）。
        真直播流没有有限总时长（ffprobe 返回 N/A 或空），此时返回 None；
        循环播放的广告/宣传短片本质是普通视频文件，会返回有限的短时长。"""
        ffprobe = getattr(self, '_ffprobe_path', None) or 'ffprobe'
        # 未显式传超时则跟随 CI/ffprobe 超时策略（避免 timeout=None 永久阻塞）
        if timeout is None:
            timeout = self.ffprobe_timeout if self.ffprobe_timeout else (
                8 if getattr(self, 'fast', False) else 20)
        try:
            result = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', url],
                capture_output=True, text=True, timeout=timeout)
            out = (result.stdout or '').strip()
            if out and out.lower() != 'n/a':
                return float(out)
        except Exception as e:
            logger.debug(f"探测媒体时长失败 {url}: {e}")
        return None

    def _is_url_playable(self, quality_info) -> bool:
        """统一的“可播放”判定：分辨率有效且缓冲状态良好。
        注意：HLS 源只有在前几个真实分片被验证可达后才会被标记为'良好'，
        因此该判定已经排除了大多数“能解析但播不了”的死源。
        对于未经 m3u8 验证的直链，额外要求下载测速确实拉到了数据，
        以剔除“能连上但不吐数据”的死源（m3u8 列表 URL 只下到几 KB 文本，
        不能用下载量判定，故通过 verified_via_m3u8 跳过该项）。"""
        MIN_DOWNLOAD_BYTES = 1024  # 15 秒下载至少应拉到 1KB 真实媒体数据
        # 伪直播短片/广告（VOD 且总时长过短）直接判定不可用
        if quality_info.get('vod_clip'):
            return False
        if quality_info['resolution'] in ['未知', '不可访问']:
            return False
        if quality_info['buffer_status'] != '良好':
            return False
        if not quality_info.get('verified_via_m3u8', False):
            if quality_info.get('total_downloaded', 0) <= MIN_DOWNLOAD_BYTES:
                return False
        # 进一步要求“稳定可长时间播放”，剔除能播但很快卡死/重缓冲的源
        if not self._is_stable(quality_info):
            return False
        return True

    def _is_stable(self, quality_info) -> bool:
        """判断直播源是否“稳定可长时间播放”，用于剔除‘能播但很快卡死/重缓冲’的源
        （典型如酒店源：开局流畅，一分钟内被限速或会话中断）。
        判定依据：
          1) 持续下载速率（kbps）需 >= ffprobe 得到的源码率 * 安全系数，否则必卡；
          2) 测试期间重缓冲次数 < 阈值，且最长无数据空窗 < 阈值。
        对 m3u8 源，速率/卡顿来自“持续拉取分片”测试；若未能测出任何数据则保守视为稳定。"""
        bitrate_kbps = None
        bitrate = quality_info.get('bitrate', '')
        if isinstance(bitrate, str):
            m = re.search(r'(\d+)\s*kbps', bitrate, re.IGNORECASE)
            if m:
                bitrate_kbps = int(m.group(1))
        speed_kbps = quality_info.get('download_speed', 0) * 8  # KB/s -> kbps

        # 有码率信息且持续速率明显低于码率：必卡
        if bitrate_kbps and speed_kbps > 0 and speed_kbps < bitrate_kbps * 1.2:
            return False

        # 频繁卡顿或出现过较长空窗：不稳定
        if quality_info.get('stall_events', 0) >= 2:
            return False
        if quality_info.get('max_gap', 0.0) >= 5.0:
            return False
        return True

    def _collect_segments(self, url: str):
        """解析播放列表，返回其媒体分片的绝对 URL 列表（支持一层主/子播放列表嵌套）。"""
        try:
            import m3u8
            import requests
            content = None
            resp = requests.get(url, timeout=10)
            for enc in ('utf-8', 'gbk', 'latin-1'):
                try:
                    content = resp.content.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if not content:
                return []
            obj = m3u8.loads(content)
            segs = getattr(obj, 'segments', None)
            if segs:
                return [urljoin(url, s.uri) for s in segs]
            pls = getattr(obj, 'playlists', None)
            if pls:
                return self._collect_segments(urljoin(url, pls[0].uri))
            return []
        except Exception as e:
            logger.debug(f"收集分片失败 {url}: {e}")
            return []

    def _test_m3u8_stability(self, url: str, duration: int):
        """对 m3u8 源按时间顺序持续拉取 TS 分片，模拟播放过程以检测卡顿/限速。
        返回 (total_downloaded, speed_kbps, stall_events, max_gap) 或 None（无法测试）。
        能抓住“开局流畅、随后被限速/会话中断”的酒店源：前期分片快、后期分片变慢即暴露。"""
        try:
            import m3u8  # noqa: F401
            import requests
            segs = self._collect_segments(url)
            if not segs:
                return None
            start = time.time()
            last_byte = start
            total = 0
            stall_events = 0
            max_gap = 0.0
            in_stall = False
            STALL = 2.0  # 超过 2 秒无数据视为一次卡顿
            n = len(segs)
            seg_idx = 0
            while time.time() - start < duration:
                seg_url = segs[seg_idx % n]
                seg_idx += 1
                try:
                    resp = requests.get(seg_url, stream=True, timeout=20)
                    last_byte = time.time()  # 重置基准，避免把“分片之间的拉取间隔”误判为卡顿
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        now = time.time()
                        if chunk:
                            total += len(chunk)
                            gap = now - last_byte
                            if gap > STALL:
                                if not in_stall:
                                    stall_events += 1
                                    in_stall = True
                                max_gap = max(max_gap, gap)
                            else:
                                in_stall = False
                            last_byte = now
                        if now - start >= duration:
                            break
                    resp.close()
                except requests.RequestException:
                    # 某个分片拉取失败/超时：计为一次卡顿
                    stall_events += 1
                    in_stall = False
                    last_byte = time.time()
            elapsed = time.time() - start
            speed = total / elapsed / 1024 if elapsed > 0 else 0
            return (total, speed, stall_events, max_gap)
        except Exception as e:
            logger.debug(f"m3u8 稳定性测试失败 {url}: {e}")
            return None

    def get_stream_info(self, url: str) -> Optional[Tuple[str, str, str, str]]:
        """使用FFmpeg获取码流信息，返回(分辨率, 码率, 延迟, 缓冲状态)"""
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
            return None
        # 缓存 ffprobe 路径，供 _probe_media_duration 等轻量探测复用
        self._ffprobe_path = ffprobe_path

        # ffprobe 子进程超时：显式参数 > fast 模式(8s) > 默认(20s)。
        # fast 模式下 CI 跑数万源，死源应尽早放弃，避免 20s 超时拖垮整体时长。
        _ff_timeout = self.ffprobe_timeout if self.ffprobe_timeout else (
            8 if getattr(self, 'fast', False) else 20)
        # ffprobe 内部协议超时（微秒），跟随子进程超时略留余量
        _ff_micro = int(_ff_timeout * 1_000_000)
        
        # 构建FFprobe命令 - 优化参数以更快获取码流信息，增加超时时间
        cmd = [
            ffprobe_path,
            '-v', 'warning',  # 显示警告信息以便调试
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,bit_rate,avg_bit_rate,start_time,duration,codec_time_base',  # 添加延迟相关字段
            '-show_entries', 'format=bit_rate,start_time,duration,probe_score',  # 添加格式延迟和缓冲相关字段
            '-of', 'csv=p=0',
            '-timeout', str(_ff_micro),  # 协议超时（单位：微秒）
            '-reconnect', '1',  # 允许重连
            '-reconnect_delay_max', '3',  # 最大重连延迟3秒
            '-reconnect_at_eof', '1',  # 允许在EOF时重连
            '-probesize', '2000000',  # 增加探针大小（2MB）
            '-analyzeduration', '5000000',  # 增加分析时长（5秒）
            '-rw_timeout', str(_ff_micro),  # 读写超时
            '-max_delay', str(_ff_micro),  # 最大延迟
            url
        ]
        
        try:
            # 执行命令
            logger.debug(f"执行ffprobe命令: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_ff_timeout,  # ffprobe 子进程超时（秒）
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
                    '-rw_timeout', str(_ff_micro),  # 读写超时
                    '-max_delay', str(_ff_micro)  # 最大延迟
                ]
                
                json_result = subprocess.run(
                    json_cmd,
                    capture_output=True,
                    text=True,
                    timeout=_ff_timeout  # 跟随同一超时
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

    @staticmethod
    def _is_playlist_url(url):
        """判断 URL 是否为 m3u8/m3u 播放列表。

        兼容带查询参数/锚点的地址，如 xxx.m3u8?auth=yyy。
        """
        lower = url.lower()
        path = lower.split('?', 1)[0].split('#', 1)[0]
        return path.endswith(('.m3u8', '.m3u')) or '.m3u8' in lower

    def analyze_channel_quality(self, channel):
        """分析单个频道的所有直播源质量信息"""
        try:
            # 为每个URL存储质量信息
            channel.quality_info_list = []
            
            # 频道内多源并行检测：将单源分析提取为闭包，结果顺序与 channel.urls 对齐
            def _analyze_url(url):
                url_quality = {
                    'resolution': '未知',
                    'bitrate': '未知',
                    'delay': '未知',
                    'buffer_status': '未知',
                    'download_speed': 0,
                    'total_downloaded': 0,
                    'download_time': 0,
                    'stall_events': 0,       # 重缓冲/卡顿次数（>2s 无数据计一次）
                    'max_gap': 0.0,          # 最长连续无数据时长（秒）
                    'verified_via_m3u8': False,  # 标记是否通过 m3u8 真实分片验证（此类源的下载字节数不可用于判定）
                    'vod_clip': False        # 标记为“伪直播”短片/广告（VOD 且总时长 < MIN_LIVE_DURATION）
                }
                
                # 首先检查URL可访问性（fast 模式缩短超时与重试，降低死源耗时）
                _acc_timeout = 4 if self.fast else 5
                _acc_retries = 1 if self.fast else 2
                if not self.check_url_accessibility(url, timeout=_acc_timeout, retries=_acc_retries):
                    url_quality['resolution'] = '不可访问'
                    return url_quality
                
                # URL可访问，设置初始状态为未知，而不是不可访问
                url_quality['resolution'] = '未知'
                url_quality['buffer_status'] = '未知'
                
                # 播放列表 URL 跳过前置 ffprobe（死源会拖满 20s 超时，且 m3u8 复检
                # + 分片验证已足够判定有效性），改为在分片验证通过后按需补测分辨率。
                is_playlist = self._is_playlist_url(url)
                if not is_playlist:
                    # 使用FFprobe获取流信息
                    stream_info = self.get_stream_info(url)
                    if stream_info:
                        url_quality['resolution'], url_quality['bitrate'], url_quality['delay'], url_quality['buffer_status'] = stream_info
                
                # 对缓冲状态和分辨率未知的直播源使用m3u库进行复检
                if url_quality['resolution'] == '未知' or url_quality['buffer_status'] == '未知':
                    logger.debug(f"对URL {url} 进行m3u库复检")
                    try:
                        import m3u8
                        import requests

                        # 尝试获取内容，处理编码问题，设置10秒超时
                        response = requests.get(url, timeout=10)
                        # 尝试不同编码
                        content = None
                        for encoding in ['utf-8', 'gbk', 'latin-1']:
                            try:
                                content = response.content.decode(encoding)
                                break
                            except UnicodeDecodeError:
                                continue

                        if content:
                            # 使用已解码的内容创建m3u8对象
                            m3u8_obj = m3u8.loads(content)

                            # 如果解析成功，说明URL是有效的m3u8流
                            if m3u8_obj:
                                logger.debug(f"m3u8库成功解析URL: {url}")

                                # 1. 检查是否有播放列表（主播放列表，包含不同分辨率选项）
                                if hasattr(m3u8_obj, 'playlists') and m3u8_obj.playlists:
                                    # 找到最高质量的播放列表
                                    max_resolution = (0, 0)
                                    best_uri = None

                                    for playlist in m3u8_obj.playlists:
                                        if hasattr(playlist, 'stream_info') and playlist.stream_info:
                                            if hasattr(playlist.stream_info, 'resolution') and playlist.stream_info.resolution:
                                                width, height = playlist.stream_info.resolution
                                                if (width, height) > max_resolution:
                                                    max_resolution = (width, height)
                                                    best_uri = playlist.uri

                                    if max_resolution != (0, 0):
                                        resolution = f"{max_resolution[0]}x{max_resolution[1]}"
                                        # 关键：递归下钻到子播放列表并验证真实分片可达
                                        child_url = urljoin(url, best_uri) if best_uri else url
                                        ok, why = self._verify_m3u8_media(child_url)
                                        if ok:
                                            url_quality['resolution'] = resolution
                                            url_quality['buffer_status'] = '良好'
                                            url_quality['verified_via_m3u8'] = True
                                            logger.debug(f"m3u8库复检检测到分辨率: {resolution}，分片验证通过")
                                        else:
                                            url_quality['buffer_status'] = '较差'
                                            if why == 'vod_clip':
                                                url_quality['vod_clip'] = True
                                                logger.debug(f"检测到伪直播短片(广告)，剔除: {url}")
                                            else:
                                                logger.debug(f"m3u8库复检检测到分辨率: {resolution}，但分片不可达")
                                    else:
                                        # 尝试从播放列表的URI中提取分辨率信息兜底
                                        for playlist in m3u8_obj.playlists:
                                            if hasattr(playlist, 'uri') and playlist.uri:
                                                uri = playlist.uri
                                                resolution_match = re.search(r'(\d+)x(\d+)', uri)
                                                if resolution_match:
                                                    width = resolution_match.group(1)
                                                    height = resolution_match.group(2)
                                                    resolution = f"{width}x{height}"
                                                    child_url = urljoin(url, playlist.uri)
                                                    ok, why = self._verify_m3u8_media(child_url)
                                                    if ok:
                                                        url_quality['resolution'] = resolution
                                                        url_quality['buffer_status'] = '良好'
                                                        url_quality['verified_via_m3u8'] = True
                                                        logger.debug(f"从播放列表URI提取到分辨率: {resolution}，分片验证通过")
                                                        break
                                                    else:
                                                        url_quality['buffer_status'] = '较差'
                                                        if why == 'vod_clip':
                                                            url_quality['vod_clip'] = True
                                                            logger.debug(f"检测到伪直播短片(广告)，剔除: {url}")

                                # 2. 检查是否有媒体段（子播放列表，包含TS片段）
                                elif hasattr(m3u8_obj, 'segments') and isinstance(m3u8_obj.segments, list) and m3u8_obj.segments:
                                    logger.debug(f"m3u8库解析成功，检测到{len(m3u8_obj.segments)}个媒体段")
                                    # 关键：验证前几个分片是否真实可达，可达才视为良好
                                    ok, why = self._verify_m3u8_media(url)
                                    if ok:
                                        url_quality['resolution'] = '有效媒体段'
                                        url_quality['buffer_status'] = '良好'
                                        url_quality['verified_via_m3u8'] = True
                                        logger.debug(f"媒体段播放列表分片验证通过，标记为有效媒体段")
                                    else:
                                        url_quality['buffer_status'] = '较差'
                                        if why == 'vod_clip':
                                            url_quality['vod_clip'] = True
                                            logger.debug(f"检测到伪直播短片(广告)，剔除: {url}")
                                        else:
                                            logger.debug(f"媒体段播放列表解析成功，但分片不可达")
                    except requests.Timeout:
                        logger.debug(f"m3u8库复检超时 {url}: 10秒超时")
                        # 超时跳出，继续下一项检测
                        # 注意：这里不要修改url_quality，保持之前的状态
                    except Exception as e:
                        logger.debug(f"m3u8库复检失败 {url}: {e}")
                        # 对于无法解析的m3u8流，继续执行，保持之前的状态
                        pass
                
                # 直链做窗口化下载测速；m3u8 已验证源改为“持续拉取分片”测稳定性。
                # 两者都会产出 download_speed / stall_events / max_gap 供稳定性判定使用。
                dead = (
                    url_quality['buffer_status'] == '较差'
                    or (url_quality['resolution'] == '未知' and url_quality['buffer_status'] == '未知')
                )
                if not dead:
                    # .m3u8/.m3u 播放列表 URL（含 ?auth= 等查询参数形式）：即使被解析出
                    # 分辨率，也应走分片级稳定性测试（直链下载只会下到几 KB 播放列表
                    # 文本，会触发最小下载字节门禁误杀）
                    if is_playlist or url_quality['verified_via_m3u8']:
                        if is_playlist:
                            url_quality['verified_via_m3u8'] = True
                        if not self.fast:
                            # 播放列表源跳过了前置 ffprobe，这里对已确认存活的源按需补测
                            # 一次，以获取真实分辨率/码率（失败不影响判定）
                            if url_quality['resolution'] in ('未知', '有效媒体段'):
                                stream_info = self.get_stream_info(url)
                                if stream_info:
                                    res, br, dl, _bs = stream_info
                                    if res != '未知':
                                        url_quality['resolution'] = res
                                    if br != '未知':
                                        url_quality['bitrate'] = br
                                    if dl != '未知':
                                        url_quality['delay'] = dl
                            # m3u8 源：按时间顺序持续拉取 TS 分片，模拟播放过程检测卡顿/限速
                            result = self._test_m3u8_stability(url, self.download_duration)
                            if result:
                                total_downloaded, speed, stall_events, max_gap = result
                                url_quality['download_speed'] = speed
                                url_quality['total_downloaded'] = total_downloaded
                                url_quality['download_time'] = self.download_duration
                                url_quality['stall_events'] = stall_events
                                url_quality['max_gap'] = round(max_gap, 2)
                    else:
                        # 窗口化采样下载，检测“开局流畅、随后卡死/重缓冲”的不稳定源
                        try:
                            import requests
                            start_time = time.time()
                            last_byte_time = start_time
                            response = requests.get(url, stream=True, timeout=15)

                            # 下载指定时长的数据
                            chunk_size = 1024 * 64  # 64KB chunks
                            total_downloaded = 0
                            stall_events = 0       # 重缓冲/卡顿次数（超过阈值无数据算一次）
                            max_gap = 0.0          # 最长连续无数据时长
                            in_stall = False
                            STALL_THRESHOLD = 2.0  # 超过 2 秒收不到任何字节视为一次卡顿
                            # fast 模式改用更短的采样时长，仍满足最小下载字节门禁
                            _sample_dur = self.fast_sample if self.fast else self.download_duration

                            for chunk in response.iter_content(chunk_size=chunk_size):
                                now = time.time()
                                if chunk:
                                    total_downloaded += len(chunk)
                                    gap = now - last_byte_time
                                    if gap > STALL_THRESHOLD:
                                        if not in_stall:
                                            stall_events += 1
                                            in_stall = True
                                        max_gap = max(max_gap, gap)
                                    else:
                                        in_stall = False
                                    last_byte_time = now
                                # 检查是否已经达到指定的下载时间
                                if now - start_time >= _sample_dur:
                                    break

                            download_time = time.time() - start_time

                            if download_time > 0:
                                url_quality['download_speed'] = total_downloaded / download_time / 1024  # KB/s
                                url_quality['total_downloaded'] = total_downloaded
                                url_quality['download_time'] = download_time
                                # 稳定性相关指标（用于判断“能播但会卡”的源）
                                url_quality['stall_events'] = stall_events
                                url_quality['max_gap'] = round(max_gap, 2)

                            response.close()

                            # 直链“伪直播”检测：真直播流没有有限总时长，
                            # 循环播放的广告短片是普通视频文件，时长有限且很短
                            if total_downloaded > 1024:
                                dur = self._probe_media_duration(url)
                                # 有限时长(点播文件)即判为伪直播剔除；真直播返回 N/A 保留
                                if self.max_live_duration > 0 and dur is not None:
                                    url_quality['vod_clip'] = True
                                    url_quality['buffer_status'] = '较差'
                                    logger.debug(f"直链检测到伪直播短片(时长{dur:.0f}s)，剔除: {url}")
                        except Exception as e:
                            logger.error(f"下载速度测试失败 {url}: {e}")
                
                # 对于可访问但分辨率和缓冲状态都是未知的URL，尝试作为有效媒体段处理
                # if url_quality['resolution'] == '未知' and url_quality['buffer_status'] == '未知' and url_quality['download_speed'] > 0:
                #     logger.debug(f"URL {url} 可访问且有下载速度，但无法获取分辨率和缓冲状态，标记为有效媒体段")
                #     url_quality['resolution'] = '有效媒体段'
                #     url_quality['buffer_status'] = '良好'
                
                return url_quality

            # 频道内多源并行检测，结果顺序与 channel.urls 保持一致
            urls = list(channel.urls)
            results: list[object] = [None] * len(urls)
            if urls:
                # 单源总时长兜底：用 submit + as_completed(timeout) 给整个频道一个硬上限，
                # 杜绝慢滴答源卡在非流式 GET 上导致频道线程无限挂死。超时/异常的源填入
                # 不可访问兜底，被 _is_url_playable 自然剔除，不影响同频道其他源。
                _src_to = self.source_timeout
                ex = concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(self.channel_workers, len(urls)))
                try:
                    future_to_idx = {ex.submit(_analyze_url, u): i for i, u in enumerate(urls)}
                    if _src_to:
                        pending = set(future_to_idx)
                        try:
                            for fut in concurrent.futures.as_completed(future_to_idx, timeout=_src_to):
                                i = future_to_idx[fut]
                                try:
                                    results[i] = fut.result()
                                except Exception as e:
                                    logger.error(f"单源检测异常 {urls[i]}: {e}")
                                    results[i] = self._unreachable_quality()
                                pending.discard(fut)
                        except concurrent.futures.TimeoutError:
                            if pending:
                                logger.warning(
                                    f"频道 {channel.name or ''} 有 {len(pending)} 个源检测超时"
                                    f"({_src_to}s)，按不可访问跳过")
                        for fut in pending:
                            results[future_to_idx[fut]] = self._unreachable_quality()
                    else:
                        for fut, i in future_to_idx.items():
                            try:
                                results[i] = fut.result()
                            except Exception as e:
                                logger.error(f"单源检测异常 {urls[i]}: {e}")
                                results[i] = self._unreachable_quality()
                finally:
                    # wait=False 不阻塞频道线程；cancel_futures 取消未启动任务。
                    # 仍卡在 requests 的孤儿线程会因各 recv 超时(4-15s)自行退出，
                    # 最终由 GitHub job 超时兜底，绝不会再阻塞频道并发槽位。
                    ex.shutdown(wait=False, cancel_futures=True)
            channel.quality_info_list = results

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
        if self.fast:
            print(f"快速模式：跳过{self.download_duration}s稳定性流式测试，直链采样"
                  f"{self.fast_sample}s，缩短超时与死源重试；适用于每日大规模(数万源)检测")
        else:
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
                        print(f"    下载速度: {quality_info['download_speed']:.2f} KB/s ({quality_info['download_speed']*8:.0f} kbps)")
                        print(f"    总下载量: {quality_info['total_downloaded']/1024:.1f} KB")
                        print(f"    总下载时间: {quality_info['download_time']:.2f} s")
                        print(f"    卡顿次数: {quality_info.get('stall_events', 0)}")
                        print(f"    最长空窗: {quality_info.get('max_gap', 0.0):.1f} s")
                        print(f"    稳定性: {'稳定' if self._is_stable(quality_info) else '不稳定'}")
                    
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
                # 获取该URL的质量信息
                url_quality = channel.quality_info_list[i] if hasattr(channel, 'quality_info_list') else channel.quality_info
                
                # 检查条件：分辨率有效且缓冲状态良好（已内含分片可达验证）
                if self._is_url_playable(url_quality):
                    has_valid_url = True
                    break
            
            if has_valid_url:
                filtered_channels.append(channel)
        
        self.channels = filtered_channels
    
    def _parse_resolution(self, resolution_str):
        """解析分辨率字符串为可比较的数值"""
        if resolution_str == '未知' or resolution_str == '不可访问':
            return (0, 0)
        
        # 有效媒体段，使用默认分辨率值
        if resolution_str == '有效媒体段':
            return (720, 480)  # 默认使用720x480作为有效媒体段的分辨率
        
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
    
    def _write_stability_report(self, path: str):
        """导出每个直播源的稳定性明细 CSV（频道/分组/分辨率/码率/实测速率/卡顿次数/最长空窗/稳定性/URL），
        方便人工复核阈值是否合理。"""
        try:
            import csv
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                w.writerow(['channel', 'group', 'resolution', 'bitrate',
                            'speed_kbps', 'stall_events', 'max_gap_s', 'stable', 'vod_clip', 'url'])
                for ch in self.channels:
                    if not hasattr(ch, 'quality_info_list'):
                        continue
                    for url, qi in zip(ch.urls, ch.quality_info_list):
                        stable = self._is_stable(qi)
                        speed_kbps = round(qi.get('download_speed', 0) * 8, 1)
                        w.writerow([
                            ch.name, ch.group_title, qi.get('resolution', ''), qi.get('bitrate', ''),
                            speed_kbps, qi.get('stall_events', 0), qi.get('max_gap', 0), stable,
                            qi.get('vod_clip', False), url
                        ])
            logger.info(f"稳定性明细已写入: {path}")
        except Exception as e:
            logger.error(f"写入稳定性报告失败: {e}")

    def process(self):
        """执行完整的处理流程"""
        print(f"开始处理M3U文件: {self.input_file}")
        
        # 1. 解析文件
        self.header_lines, self.channels = self.parse_m3u_file()
        original_channel_count = len(self.channels)
        print(f"解析到 {original_channel_count} 个频道")

        # 1.5 CI 分块处理：仅保留前 N 个频道，避免单次运行处理数万源导致 runner 超时
        if self.max_channels and self.max_channels > 0 and len(self.channels) > self.max_channels:
            print(f"--max-channels={self.max_channels}：仅处理前 {self.max_channels} 个频道"
                  f"（其余 {len(self.channels) - self.max_channels} 个本次跳过）")
            self.channels = self.channels[:self.max_channels]

        if not self.channels:
            print("未找到任何频道，程序退出")
            return
        
        # 2. 分析频道质量
        self.analyze_all_channels()

        # 2.5 先导出稳定性明细报告（包含所有已分析频道），便于人工复核阈值
        self._write_stability_report(self.output_file + '.stability.csv')

        # 2.6 先剔除“没有任何可播放URL”的频道（统一调用 filter_channels 复用判定逻辑）
        self.filter_channels()
        
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
                    if self._is_url_playable(quality_info):
                        valid_url_indices.append(i)
                        has_valid_url = True
                        valid_url_count += 1

            # 按“稳定性优先、其次下载速度”排序，让稳定且更快的源排在前面（播放器优先尝试）
            if hasattr(channel, 'quality_info_list'):
                valid_url_indices.sort(
                    key=lambda i: (1 if self._is_stable(channel.quality_info_list[i]) else 0,
                                   channel.quality_info_list[i].get('download_speed', 0)),
                    reverse=True
                )

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
    parser.add_argument('-c', '--channel-workers', type=int, default=8,
                        help='单频道内多源并行检测的并发数（与 -t 相互独立）')
    parser.add_argument('--fast', action='store_true',
                        help='快速模式：跳过15s稳定性流式测试、缩短超时与死源重试、'
                             'm3u8仅验证首分片，单源耗时从~25s降到~7s，适合每日数万源检测')
    parser.add_argument('--fast-sample', type=int, default=3,
                        help='快速模式下直链采样下载时长(秒)，默认3')
    parser.add_argument('--max-live-duration', type=int, default=None,
                        help='伪直播(固定时长点播:VOD/电影/MTV/风景/广告)剔除开关(秒)。'
                             '>0 时凡 ffprobe 探测到“有限总时长”的源一律剔除(真24/7直播无有限时长不受影响)；'
                             '默认开启(3600占位)；设为0可关闭该剔除')
    parser.add_argument('--max-channels', type=int, default=0,
                        help='最多处理前 N 个频道(0=不限制)。用于 CI 分块处理，'
                             '避免单次运行处理数万源导致 runner 超时')
    parser.add_argument('--ffprobe-timeout', type=int, default=0,
                        help='ffprobe 调用超时秒数(0=沿用脚本默认)。CI 中建议设小(如8)以快速放弃死源')
    parser.add_argument('--source-timeout', type=int, default=60,
                        help='单源检测总时长上限秒数(0=不限制)；杜绝慢滴答源卡死频道并发槽位')
    args = parser.parse_args()

    # --max-live-duration 为 0 表示关闭固定时长剔除
    _max_live = None if (args.max_live_duration is None or args.max_live_duration <= 0) \
        else args.max_live_duration

    # --ffprobe-timeout 为 0 表示沿用脚本内部默认；显式传入时覆盖
    _ffprobe_timeout = args.ffprobe_timeout if args.ffprobe_timeout and args.ffprobe_timeout > 0 \
        else None

    _exit_code = 0
    try:
        processor = M3UProcessor(
            input_file=args.input_file,
            output_file=args.output,
            max_threads=args.threads,
            download_duration=args.duration,
            channel_workers=args.channel_workers,
            fast=args.fast,
            fast_sample=args.fast_sample,
            max_live_duration=_max_live,
            max_channels=args.max_channels,
            ffprobe_timeout=_ffprobe_timeout,
            source_timeout=args.source_timeout
        )
        processor.process()

    except Exception as e:
        print(f"处理失败: {e}")
        _exit_code = 1

    # ThreadPoolExecutor 的 atexit handler(_python_exit)会 join 所有 worker 线程，
    # 包括 shutdown(wait=False) 留下的、卡在 requests 慢滴答源上的孤儿线程，
    # 导致工作已完成、产物已落盘(.m3u/.stability.csv 均经 with open() 写闭)但
    # 进程无法退出，后续 workflow 步骤(Show files / Upload)永远不执行。
    # os._exit 绕过 atexit 与解释器清理，确保产物落盘后立即退出。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_exit_code)

if __name__ == "__main__":
    main()
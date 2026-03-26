#!/usr/bin/env python3
"""网易云单曲下载器。"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import aiofiles
import aiohttp
import httpx
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TIT2, TPE1, USLT, ID3

API_BASE_URL = "https://163api.qijieya.cn"
METING_API = "https://api.qijieya.cn/meting/"
SEARCH_RESULTS_COUNT = 5
DOWNLOAD_TIMEOUT = 30
CHUNK_SIZE = 64 * 1024
MIN_FILE_SIZE = 1024
COVER_SIZE = 800
MUSIC_DIR = Path("music")
DEFAULT_QUALITY: Literal["flac", "mp3"] = "flac"


@dataclass
class SongInfo:
    id: int
    name: str
    artists: str
    album: str
    duration_ms: int


@dataclass
class LyricsInfo:
    lyric: str = ""
    translated_lyric: str = ""


class DownloadError(Exception):
    """下载异常。"""


def detect_audio_format(file_path: Path) -> Literal["flac", "mp3"]:
    with file_path.open("rb") as file:
        header = file.read(4)

    if header.startswith(b"fLaC"):
        return "flac"
    if header[:3] == b"ID3" or header[:2] == b"\xff\xfb" or header[:2] == b"\xff\xf3" or header[:2] == b"\xff\xf2":
        return "mp3"
    raise DownloadError("下载文件不是有效的 FLAC 或 MP3 音频")


class NetEaseClient:
    def __init__(self, base_url: str = API_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            timeout=10.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/132.0.0.0 Safari/537.36"
                )
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def search_songs(self, keyword: str, limit: int = SEARCH_RESULTS_COUNT) -> list[SongInfo]:
        response = await self.client.get(
            f"{self.base_url}/cloudsearch",
            params={"keywords": keyword, "type": 1, "limit": limit, "offset": 0},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code", 200) != 200:
            raise DownloadError(f"搜索失败，接口返回 code={payload.get('code')}")

        songs = payload.get("result", {}).get("songs", [])
        return [
            SongInfo(
                id=item.get("id"),
                name=item.get("name", "未知歌曲"),
                artists=format_artists(item.get("ar", [])),
                album=item.get("al", {}).get("name", "未知专辑"),
                duration_ms=item.get("dt", 0),
            )
            for item in songs
        ]

    async def get_song_url(self, song_id: int, br: str) -> str:
        response = await self.client.get(
            METING_API,
            params={"type": "url", "id": song_id, "br": br},
            follow_redirects=True,
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = response.json()
            if isinstance(payload, dict):
                if payload.get("url"):
                    return payload["url"]
                if payload.get("freeTrialInfo"):
                    raise DownloadError("当前音质仅提供试听片段，无法下载完整歌曲")
            raise DownloadError(f"未获取到歌曲链接，br={br}")

        direct_url = str(response.url)
        if direct_url:
            return direct_url
        raise DownloadError(f"未获取到歌曲链接，br={br}")

    async def get_cover_url(self, song_id: int) -> str:
        try:
            response = await self.client.get(METING_API, params={"type": "song", "id": song_id})
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list) and payload:
                redirect_url = payload[0].get("pic", "")
                if redirect_url:
                    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as redirect_client:
                        redirected = await redirect_client.get(redirect_url)
                        return str(redirected.url)
        except Exception:
            return ""
        return ""

    async def get_lyrics(self, song_id: int) -> LyricsInfo:
        try:
            response = await self.client.get(METING_API, params={"type": "lrc", "id": song_id})
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                payload = response.json()
                if isinstance(payload, dict):
                    return LyricsInfo(
                        lyric=payload.get("lyric", "") or payload.get("lrc", ""),
                        translated_lyric=payload.get("tlyric", "") or payload.get("trans", ""),
                    )

            text = response.text.strip()
            return LyricsInfo(lyric=text)
        except Exception as exc:
            logger.warning("歌词获取失败：%s", exc)
            return LyricsInfo()


class FileDownloader:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.close()
            self.session = None

    async def download(self, url: str, output_path: Path) -> None:
        if not self.session:
            raise DownloadError("下载会话未初始化")

        async with self.session.get(url) as response:
            response.raise_for_status()
            total_size = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            async with aiofiles.open(output_path, "wb") as file:
                async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                    await file.write(chunk)
                    downloaded += len(chunk)
                    print_progress(downloaded, total_size)
            if total_size:
                print()

    async def get_bytes(self, url: str) -> bytes:
        if not self.session:
            raise DownloadError("下载会话未初始化")

        async with self.session.get(url) as response:
            response.raise_for_status()
            return await response.read()


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    return logging.getLogger(__name__)


logger = setup_logging()


def format_artists(artists: list[dict[str, Any]]) -> str:
    if not artists:
        return "未知"
    return "/".join(artist.get("name", "未知") for artist in artists)


def format_duration(duration_ms: int) -> str:
    seconds = max(duration_ms // 1000, 0)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def sanitize_filename(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", text).strip()


def print_progress(downloaded: int, total_size: int) -> None:
    if total_size > 0:
        percent = downloaded / total_size
        bar_length = 30
        filled = int(bar_length * percent)
        bar = "#" * filled + "-" * (bar_length - filled)
        print(
            f"\r下载进度：[{bar}] {percent * 100:6.2f}% ({downloaded / 1024 / 1024:.2f}MB/{total_size / 1024 / 1024:.2f}MB)",
            end="",
            flush=True,
        )
    else:
        print(f"\r已下载：{downloaded / 1024 / 1024:.2f}MB", end="", flush=True)


def print_search_results(songs: list[SongInfo]) -> None:
    print(f"\n搜索结果（前 {SEARCH_RESULTS_COUNT} 首）：")
    for index, song in enumerate(songs, start=1):
        print(
            f"{index}. {song.name} - {song.artists} "
            f"| 专辑：{song.album} | 时长：{format_duration(song.duration_ms)}"
        )


def ask_quality() -> Literal["flac", "mp3"]:
    print("请选择音质：")
    print("1. FLAC 无损")
    print("2. MP3 320kbps")
    selected = input("输入序号（默认 1）: ").strip()
    return "mp3" if selected == "2" else DEFAULT_QUALITY


def ask_song_choice(songs: list[SongInfo]) -> SongInfo:
    while True:
        choice = input("请选择要下载的歌曲序号: ").strip()
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(songs):
                return songs[index - 1]
        print("输入无效，请重新输入。")


async def resolve_download_url(client: NetEaseClient, song_id: int, quality: Literal["flac", "mp3"]):
    br_map = {"flac": "2000", "mp3": "320"}
    if quality == "mp3":
        return await client.get_song_url(song_id, br_map["mp3"]), "mp3"

    try:
        url = await client.get_song_url(song_id, br_map["flac"])
        if url.lower().endswith(".mp3"):
            logger.warning("请求 FLAC 但接口返回 MP3，已自动按 MP3 处理")
            return url, "mp3"
        return url, "flac"
    except DownloadError:
        logger.warning("FLAC 获取失败，自动降级为 MP3")
        return await client.get_song_url(song_id, br_map["mp3"]), "mp3"


async def write_metadata(
    file_path: Path,
    song: SongInfo,
    cover_bytes: bytes | None,
    lyrics: LyricsInfo,
    file_type: Literal["flac", "mp3"],
) -> None:
    if file_type == "flac":
        audio = FLAC(file_path)
        audio["title"] = song.name
        audio["artist"] = song.artists
        audio["album"] = song.album
        if lyrics.lyric:
            audio["lyrics"] = lyrics.lyric
        if lyrics.translated_lyric:
            audio["translyrics"] = lyrics.translated_lyric
        if cover_bytes:
            picture = Picture()
            picture.type = 3
            picture.mime = "image/jpeg"
            picture.desc = "Cover"
            picture.data = cover_bytes
            audio.clear_pictures()
            audio.add_picture(picture)
        audio.save()
        return

    try:
        audio = ID3(file_path)
    except Exception:
        audio = ID3()
    audio.delall("APIC")
    audio.delall("TIT2")
    audio.delall("TPE1")
    audio.delall("TALB")
    audio.delall("USLT")
    audio.add(TIT2(encoding=3, text=song.name))
    audio.add(TPE1(encoding=3, text=song.artists))
    audio.add(TALB(encoding=3, text=song.album))
    if lyrics.lyric:
        audio.add(USLT(encoding=3, lang="chi", desc="Lyrics", text=lyrics.lyric))
    if cover_bytes:
        audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
    audio.save(file_path, v2_version=3)


async def run_download(keyword: str, quality: Literal["flac", "mp3"]) -> Path:
    client = NetEaseClient()
    output_path: Path | None = None
    try:
        songs = await client.search_songs(keyword, limit=SEARCH_RESULTS_COUNT)
        if not songs:
            raise DownloadError("未搜索到相关歌曲")

        print_search_results(songs)
        selected_song = ask_song_choice(songs)
        song_url, actual_quality = await resolve_download_url(client, selected_song.id, quality)
        cover_url = await client.get_cover_url(selected_song.id)
        lyrics = await client.get_lyrics(selected_song.id)

        # print(f"目标音质：{quality.upper()}")
        # print(f"最终音频地址：{song_url}")
        # print(f"接口返回格式：{actual_quality.upper()}")

        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        temp_extension = "flac" if actual_quality == "flac" else "mp3"
        temp_filename = sanitize_filename(f"{selected_song.name} - {selected_song.artists}.{temp_extension}")
        output_path = MUSIC_DIR / temp_filename

        async with FileDownloader() as downloader:
            await downloader.download(song_url, output_path)
            if output_path.stat().st_size < MIN_FILE_SIZE:
                raise DownloadError("下载文件过小，疑似下载失败")

            detected_format = detect_audio_format(output_path)
            if detected_format != actual_quality:
                print(f"检测到实际文件格式为：{detected_format.upper()}（已自动修正扩展名）")
                corrected_name = sanitize_filename(f"{selected_song.name} - {selected_song.artists}.{detected_format}")
                corrected_path = MUSIC_DIR / corrected_name
                output_path = output_path.rename(corrected_path)
                actual_quality = detected_format

            print(f"实际下载格式：{actual_quality.upper()}")

            cover_bytes = None
            if cover_url:
                try:
                    cover_bytes = await downloader.get_bytes(f"{cover_url}?param={COVER_SIZE}y{COVER_SIZE}")
                except Exception as exc:
                    logger.warning("封面下载失败：%s", exc)

            if lyrics.lyric:
                # print("已获取歌词并写入标签")
                pass

            await write_metadata(output_path, selected_song, cover_bytes, lyrics, actual_quality)

        return output_path
    except Exception:
        if output_path and output_path.exists():
            try:
                output_path.unlink()
                logger.warning("已删除损坏文件：%s", output_path)
            except Exception as cleanup_error:
                logger.warning("清理失败文件时出错：%s", cleanup_error)
        raise
    finally:
        await client.close()


async def main() -> None:
    print("=== 网易云单曲下载器 ===")
    quality = ask_quality()
    while True:
        keyword = input("请输入歌曲关键词: ").strip()
        if not keyword:
            print("关键词不能为空，请重新输入。\n")
            continue

        try:
            output_path = await run_download(keyword, quality)
            print(f"下载完成：{output_path}")
            print()
        except KeyboardInterrupt:
            print("\n已取消下载")
            return
        except Exception as exc:
            logger.error("下载失败：%s", exc)
            print(f"下载失败：{exc}\n")


if __name__ == "__main__":
    asyncio.run(main())

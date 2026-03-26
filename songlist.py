#!/usr/bin/env python3
"""网易云歌单批量下载器。"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

import aiofiles
import aiohttp
import httpx
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TIT2, TPE1, USLT, ID3

METING_API = "https://api.qijieya.cn/meting/"
DOWNLOAD_TIMEOUT = 30
CHUNK_SIZE = 64 * 1024
MIN_FILE_SIZE = 1024
COVER_SIZE = 800
MUSIC_DIR = Path("music")
DEFAULT_QUALITY: Literal["flac", "mp3"] = "flac"
MAX_CONCURRENT_DOWNLOADS = 5


@dataclass
class LyricsInfo:
    lyric: str = ""
    translated_lyric: str = ""


@dataclass
class PlaylistSong:
    song_id: int
    name: str
    artist: str
    url: str
    pic: str
    lrc: str


class DownloadError(Exception):
    """下载异常。"""


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


def sanitize_filename(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", text).strip()


def format_size(size: int) -> str:
    return f"{size / 1024 / 1024:.2f}MB"


def detect_audio_format(file_path: Path) -> Literal["flac", "mp3"]:
    with file_path.open("rb") as file:
        header = file.read(4)
    if header.startswith(b"fLaC"):
        return "flac"
    if header[:3] == b"ID3" or header[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    raise DownloadError("下载文件不是有效的 FLAC 或 MP3 音频")


def extract_id_from_url_or_text(text: str) -> str:
    text = text.strip()
    if text.isdigit():
        return text

    id_match = re.search(r"(?:playlist\?id=|playlist/|\bid=)(\d+)", text)
    if id_match:
        return id_match.group(1)

    url_match = re.search(r"https?://[^\s)]+", text)
    if url_match:
        candidate_url = url_match.group(0)
        parsed = urlparse(candidate_url)
        query = parse_qs(parsed.query)
        if "id" in query and query["id"]:
            return query["id"][0]

    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    if "id" in query and query["id"]:
        return query["id"][0]

    raise DownloadError("无法从输入内容中提取歌单 ID")


def extract_song_id_from_url(url: str) -> int:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "id" in query and query["id"]:
        return int(query["id"][0])
    match = re.search(r"id=(\d+)", url)
    if match:
        return int(match.group(1))
    raise DownloadError(f"无法提取歌曲 ID: {url}")


class NetEasePlaylistClient:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=15.0,
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

    async def get_playlist(self, playlist_id: str) -> list[PlaylistSong]:
        response = await self.client.get(METING_API, params={"type": "playlist", "id": playlist_id})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            raise DownloadError("未获取到歌单数据")

        songs: list[PlaylistSong] = []
        for item in payload:
            songs.append(
                PlaylistSong(
                    song_id=extract_song_id_from_url(item.get("url", "")),
                    name=item.get("name", "未知歌曲"),
                    artist=item.get("artist", "未知歌手"),
                    url=item.get("url", ""),
                    pic=item.get("pic", ""),
                    lrc=item.get("lrc", ""),
                )
            )
        return songs

    async def get_song_url(self, song_id: int, quality: Literal["flac", "mp3"]) -> tuple[str, Literal["flac", "mp3"]]:
        br = "2000" if quality == "flac" else "320"
        response = await self.client.get(
            METING_API,
            params={"type": "url", "id": song_id, "br": br},
            follow_redirects=True,
        )
        response.raise_for_status()
        final_url = str(response.url)
        if not final_url:
            raise DownloadError(f"未获取到歌曲链接: {song_id}")
        if quality == "flac" and final_url.lower().endswith(".mp3"):
            return final_url, "mp3"
        return final_url, quality

    async def get_cover_bytes(self, url: str) -> bytes | None:
        if not url:
            return None
        try:
            response = await self.client.get(url, follow_redirects=True)
            response.raise_for_status()
            return response.content
        except Exception:
            return None

    async def get_lyrics(self, url: str) -> LyricsInfo:
        if not url:
            return LyricsInfo()
        try:
            response = await self.client.get(url, follow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                payload = response.json()
                if isinstance(payload, dict):
                    return LyricsInfo(
                        lyric=payload.get("lyric", "") or payload.get("lrc", ""),
                        translated_lyric=payload.get("tlyric", "") or payload.get("trans", ""),
                    )
            return LyricsInfo(lyric=response.text.strip())
        except Exception:
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

    async def download(self, url: str, output_path: Path, index: int, total: int, song_name: str) -> None:
        if not self.session:
            raise DownloadError("下载会话未初始化")

        async with self.session.get(url) as response:
            response.raise_for_status()
            total_size = int(response.headers.get("Content-Length", 0))
            print(f"开始下载 [{index}/{total}] {song_name} ({format_size(total_size) if total_size else '大小未知'})")
            async with aiofiles.open(output_path, "wb") as file:
                async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                    await file.write(chunk)
            print(f"下载完成 [{index}/{total}] {song_name}")


async def write_metadata(
    file_path: Path,
    song: PlaylistSong,
    cover_bytes: bytes | None,
    lyrics: LyricsInfo,
    file_type: Literal["flac", "mp3"],
) -> None:
    title = song.name
    artist = song.artist
    album = "歌单下载"

    if file_type == "flac":
        audio = FLAC(file_path)
        audio["title"] = title
        audio["artist"] = artist
        audio["album"] = album
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
    audio.add(TIT2(encoding=3, text=title))
    audio.add(TPE1(encoding=3, text=artist))
    audio.add(TALB(encoding=3, text=album))
    if lyrics.lyric:
        audio.add(USLT(encoding=3, lang="chi", desc="Lyrics", text=lyrics.lyric))
    if cover_bytes:
        audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
    audio.save(file_path, v2_version=3)


async def download_playlist_song(
    downloader: FileDownloader,
    client: NetEasePlaylistClient,
    song: PlaylistSong,
    quality: Literal["flac", "mp3"],
    folder: Path,
    index: int,
    total: int,
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        actual_url, actual_quality = await client.get_song_url(song.song_id, quality)
        extension = "flac" if actual_quality == "flac" else "mp3"
        output_path = folder / sanitize_filename(f"{song.name} - {song.artist}.{extension}")

        try:
            await downloader.download(actual_url, output_path, index, total, song.name)
            if output_path.stat().st_size < MIN_FILE_SIZE:
                raise DownloadError("下载文件过小，疑似下载失败")

            detected_format = detect_audio_format(output_path)
            if detected_format != actual_quality:
                corrected_path = folder / sanitize_filename(f"{song.name} - {song.artist}.{detected_format}")
                output_path = output_path.rename(corrected_path)
                actual_quality = detected_format

            cover_bytes = await client.get_cover_bytes(song.pic)
            lyrics = await client.get_lyrics(song.lrc)
            await write_metadata(output_path, song, cover_bytes, lyrics, actual_quality)
            print(f"完成：{song.name} - {song.artist} [{actual_quality.upper()}]")
        except Exception as exc:
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            print(f"失败：{song.name} - {song.artist}，原因：{exc}")


async def main() -> None:
    print("=== 网易云歌单批量下载器 ===")
    print("支持输入歌单 ID 或网易云歌单分享链接")
    quality = ask_quality()

    while True:
        raw = input("请输入歌单ID或分享链接: ").strip()
        if not raw:
            print("输入不能为空，请重新输入。\n")
            continue

        try:
            playlist_id = extract_id_from_url_or_text(raw)
            break
        except Exception as exc:
            print(f"输入无效：{exc}\n")

    client = NetEasePlaylistClient()
    try:
        songs = await client.get_playlist(playlist_id)
        print(f"歌单解析成功，共 {len(songs)} 首歌曲")

        print(f"\n歌单 ID：{playlist_id}")
        print("预览前 10 首：")
        for index, song in enumerate(songs[:10], start=1):
            print(f"{index}. {song.name} - {song.artist}")
        if len(songs) > 10:
            print(f"... 其余 {len(songs) - 10} 首未展开")

        confirm = input("确认开始下载吗？(y/n，默认 y): ").strip().lower()
        if confirm not in {"", "y", "yes"}:
            print("已取消下载")
            return

        folder = MUSIC_DIR / sanitize_filename(f"playlist_{playlist_id}")
        folder.mkdir(parents=True, exist_ok=True)

        async with FileDownloader() as downloader:
            total = len(songs)
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
            tasks = [
                download_playlist_song(downloader, client, song, quality, folder, index, total, semaphore)
                for index, song in enumerate(songs, start=1)
            ]
            await asyncio.gather(*tasks)

        print(f"\n歌单下载完成，保存目录：{folder}")
    except KeyboardInterrupt:
        print("\n已取消下载")
    except Exception as exc:
        print(f"下载失败：{exc}")
    finally:
        await client.close()


def ask_quality() -> Literal["flac", "mp3"]:
    print("请选择音质：")
    print("1. FLAC 无损")
    print("2. MP3 320kbps")
    selected = input("输入序号（默认 1）: ").strip()
    return "mp3" if selected == "2" else DEFAULT_QUALITY


if __name__ == "__main__":
    asyncio.run(main())

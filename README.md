# 网易云音乐下载

## 功能特点

- **单曲下载**：输入关键词搜索歌曲，固定展示前 5 条结果
- **歌单下载**：支持输入歌单 ID 或网易云歌单分享链接批量下载
- **音质选择**：支持 `FLAC` / `MP3 320kbps`
- **自动降级**：请求 `FLAC` 失败时自动切换到 `MP3`
- **元数据写入**：自动写入标题、歌手、专辑、歌词、封面等信息
- **异步下载**：基于异步请求实现更流畅的下载体验

## 环境要求

- Python 3.10+
- Windows / macOS / Linux

## 安装

### 1. 克隆项目

```bash
git clone https://github.com/tooplick/netease-music-download
cd netease-music-download
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

## 使用方法

### 1. 单曲下载

```bash
python song.py
```

使用流程：

1. 选择音质（默认 `FLAC`）
2. 输入歌曲关键词
3. 从前 5 条搜索结果中选择要下载的歌曲
4. 程序自动下载并写入元数据

### 2. 歌单下载

```bash
python songlist.py
```

使用流程：

1. 选择音质（默认 `FLAC`）
2. 输入歌单 ID 或网易云歌单分享链接
3. 确认歌单预览信息
4. 程序批量下载歌单歌曲到独立文件夹

## 输出说明

- 单曲下载默认保存到 `music/` 目录
- 歌单下载默认保存到 `music/playlist_<歌单ID>/` 目录

## 配置参数说明

### `song.py`

- `SEARCH_RESULTS_COUNT = 5`：搜索结果展示数量
- `DOWNLOAD_TIMEOUT = 30`：下载超时时间（秒）
- `CHUNK_SIZE = 64 * 1024`：分块下载大小
- `MIN_FILE_SIZE = 1024`：最小文件大小校验阈值
- `COVER_SIZE = 800`：封面尺寸
- `MUSIC_DIR = Path("music")`：下载目录
- `DEFAULT_QUALITY = "flac"`：默认音质

### `songlist.py`

- `MAX_CONCURRENT_DOWNLOADS = 5`：并发下载数量
- 其余下载、封面、目录相关参数与单曲脚本基本一致

## 文件说明

- `song.py`：单曲搜索下载脚本
- `songlist.py`：歌单批量下载脚本
- `requirements.txt`：项目依赖列表
- `music/`：下载输出目录

## 注意事项

- 外部接口可用性会直接影响搜索、解析和下载结果
- 部分歌曲可能因版权、音源或接口限制无法返回 `FLAC`
- 当接口返回的真实格式与请求格式不一致时，程序会自动修正文件扩展名
- 请仅将本项目用于学习与研究用途

## 参考项目

- `netease-api-plugin`

## 免责声明

- 本项目仅供学习与交流使用，请勿用于商业或侵权用途
- 请遵守相关法律法规，并支持正版音乐
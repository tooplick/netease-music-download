# 网易云单曲下载器

基于 `D:\Github\netease-api-plugin` 的数据源实现网易云单曲下载，交互逻辑参考 `D:\Github\qq-music-download`。

## 功能

- 关键词搜索歌曲
- 固定显示前 5 条搜索结果
- 支持 `FLAC` / `MP3`
- `FLAC` 失败自动降级到 `MP3`
- 自动写入标题、歌手、专辑、封面元数据

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python downloader.py
```

下载文件默认保存到 `music/` 目录。
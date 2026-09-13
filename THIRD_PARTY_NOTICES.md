# 第三方许可说明

**简体中文** | [English](THIRD_PARTY_NOTICES.en.md)

Footboy（足小子）自身的代码采用根目录 [MIT License](LICENSE)。该许可不替代第三方组件的许可，也不授予输入媒体的版权或传播权。

## hls.js

| 项目 | 信息 |
| --- | --- |
| 组件 | hls.js 1.6.13 |
| 上游 | <https://github.com/video-dev/hls.js/tree/v1.6.13> |
| 本地文件 | `src/footboy/serve/static/vendor/hls.min.js` |
| 许可 | Apache-2.0 |
| 修改 | 未修改，与上游发布文件逐字节一致 |
| SHA-256 | `7c47cd97d7a6e7b98d9623dd8ed9a6d45af4be4085e0c2001cd7175c2b4cfb07` |

保留的[上游版权与许可声明](src/footboy/serve/static/vendor/hls.LICENSE)：

- Copyright (c) 2017 Dailymotion
- Copyright (c) 2013-2015 Brightcove

[Apache License 2.0 全文](src/footboy/serve/static/vendor/Apache-2.0.txt)一并分发。更新组件时须重新核对版权、许可及 NOTICE 要求。

Python 包 `License-Expression` 使用 `MIT AND Apache-2.0`，反映发行包同时包含项目代码与 hls.js。

## 运行时依赖

Python 依赖由安装工具获取，FFmpeg、ffprobe、Tesseract 及浏览器由用户安装，相关许可以实际安装版本为准。

若制作包含这些组件的发行物（容器、单文件程序等），须另行核对所有依赖的许可义务，尤其是 FFmpeg 的 LGPL/GPL 条款。不能仅依据本仓库 MIT 许可判断整个组合发行物的授权。

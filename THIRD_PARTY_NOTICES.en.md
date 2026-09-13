# Third-party notices

[简体中文](THIRD_PARTY_NOTICES.md) | **English**

Footboy's own code is covered by the root [MIT License](LICENSE). That license does not replace third-party licenses or grant copyright or redistribution rights to input media.

## hls.js

| Item | Details |
| --- | --- |
| Component | hls.js 1.6.13 |
| Upstream | <https://github.com/video-dev/hls.js/tree/v1.6.13> |
| Local file | `src/footboy/serve/static/vendor/hls.min.js` |
| License | Apache-2.0 |
| Modifications | None; identical to the upstream release file |
| SHA-256 | `7c47cd97d7a6e7b98d9623dd8ed9a6d45af4be4085e0c2001cd7175c2b4cfb07` |

The retained [upstream copyright and license notice](src/footboy/serve/static/vendor/hls.LICENSE) includes:

- Copyright (c) 2017 Dailymotion
- Copyright (c) 2013-2015 Brightcove

The complete [Apache License 2.0](src/footboy/serve/static/vendor/Apache-2.0.txt) is distributed alongside it. Recheck copyright, license, and NOTICE requirements when updating the component.

The Python package uses the license expression `MIT AND Apache-2.0` because the distribution includes both Footboy code and hls.js.

## Runtime dependencies

Python dependencies are obtained by the package installer. FFmpeg, ffprobe, Tesseract, and browsers are installed separately by the user. Their licenses depend on the actual versions installed.

Distributions that bundle these components, such as containers or standalone executables, must separately satisfy each dependency's license obligations, including the relevant FFmpeg LGPL/GPL terms. The repository's MIT license alone does not determine the licensing of a combined distribution.

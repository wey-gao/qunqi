# 支持的文件格式

本文说明群栖网关（server.py）与内置页面（app.html）对各类文件的识别、预览与存储行为。文中格式清单与代码中的 `IMAGE_EXT`、`VIDEO_EXT`、`AUDIO_EXT`、`DOC_EXT`、`ARCHIVE_EXT`、`LEGACY_OFFICE`、`MAGIC_BY_EXT` 保持一致；改代码时请同步更新本文。

## 1. 总览

| 类别 | 判定依据 | 聊天内表现 | 大小上限 |
| --- | --- | --- | --- |
| 图片 | 扩展名命中 `IMAGE_EXT` | 气泡内直接显示，可点开看原图，可设为表情，可进编辑器 | 普通 300MB，管理员可超 |
| 视频 | 扩展名命中 `VIDEO_EXT` | 气泡内 `<video>` 播放，带自定义全屏按钮 | 同上 |
| 音频 | 扩展名命中 `AUDIO_EXT` | 气泡内 `<audio>` 播放 | 同上 |
| 文档/文本 | 扩展名命中 `DOC_EXT` | 文件卡片，「预览」看提取出的文本，「下载」取原文件 | 同上 |
| 压缩包 | 扩展名命中 `ARCHIVE_EXT` | 文件卡片，`zip` 可预览前 30 个条目名 | 同上 |
| 其他 | 未命中以上集合 | 文件卡片，只能下载 | 同上 |

服务端只按扩展名做分类，不读内容判断类别；扩展名与文件头的一致性由 `STRICT_FILE_MAGIC` 单独校验（见第 7 节）。

## 2. 图片

### 2.1 支持扩展名

`png`、`jpg`、`jpeg`、`webp`、`gif`、`bmp`、`tif`、`tiff`、`avif`、`jfif`、`ico`

### 2.2 存储与显示

- 原图按上传字节原样落盘，不重编码、不缩放，文件名保留原扩展名。
- 聊天列表、头像、表情用 `/thumb/{fid}?w=128` 生成缩略图；源图带透明通道（`RGBA`、`LA`、调色板含 `transparency`）时缩略图输出 PNG，`Content-Type: image/png`；其余输出 JPEG。
- 图片消息气泡直接引用 `/media/{fid}` 原图，因此透明通道、EXIF 方向、动图帧都按原文件呈现。
- 客户端发图前会进编辑器：画质分「高清 / 标准 / 省流」，PNG/WebP/GIF 一律以 PNG 导出（不传质量参数）以保住 alpha；管理员额外有「原图」档，直接透传原始 Blob，跳过缩放与重绘。

### 2.3 特殊处理

- `heic` / `heif`：允许上传，落盘后尝试转成 JPEG（依赖运行环境的 HEIC 解码库），成功后按 `.jpg` 记录，转换失败则保留原文件按普通文件处理。
- `ico`：可上传可下载，缩略图按普通位图处理。
- `svg`：**不在图片集合内**，按危险内联类型强制下载（见第 7 节），不会在页面里当图片渲染。

## 3. 视频

支持扩展名：`mp4`、`webm`、`mov`、`mkv`、`m4v`、`3gp`

- 是否能在 WebView 里直接播，取决于容器解码能力：`mp4`（H.264/AAC）兼容最好，`webm` 视机型而定，`mkv`、`mov`、`3gp`、`m4v` 部分机型只出声音或需外部播放器。
- 服务端不做转码，播不了就是播不了，只保证原文件可下载。
- 上传大视频建议走分片接口（`/api/files/chunk/init` → `finish`），避免单请求超时。

## 4. 音频

支持扩展名：`mp3`、`wav`、`m4a`、`ogg`、`flac`、`aac`、`opus`、`amr`、`aiff`

- 气泡内用原生 `<audio>` 播放，`preload="metadata"`。
- 语音消息由客户端录音生成（WebM/Opus 或平台默认容器），与上述格式共用同一套上传与播放逻辑。
- `amr`、`aiff` 在多数 WebView 中不可播，仅保证下载。

## 5. 文档与文本

### 5.1 新版 Office 与 PDF（可提取正文）

| 扩展名 | 提取方式 | 提取范围 |
| --- | --- | --- |
| `pdf` | pypdf 逐页 `extract_text()` | 全部页，按 `[第N页]` 分段；扫描件无文本层则为空 |
| `docx` | python-docx 遍历段落 + 表格 | 全部段落；每张表最多 60 行，转成 Markdown 表格 |
| `xlsx` | openpyxl `read_only` | 前 5 个工作表，每表最多 80 行、每行前 12 列 |
| `pptx` | python-pptx 遍历形状文本 | 全部幻灯片，按 `[第N页]` 分段 |

PDF 额外支持 `<a href="/api/files/{fid}?inline=1">` 在页面内直接打开（唯一允许 inline 的类型，且要求 MIME 为 `application/pdf` 与扩展名一致）。

### 5.2 纯文本与结构化文本（按字节直读）

`txt`、`md`、`markdown`、`csv`、`tsv`、`json`、`jsonl`、`log`、`xml`、`yaml`、`yml`、`toml`、`ini`、`conf`、`cfg`

- 以 UTF-8 解码（非法字节替换），最多读前 2MB，超出追加「文件较大，仅解析前 2MB」。
- 不做语法高亮与结构化解析，预览就是原文。

### 5.3 代码文件（同上，按纯文本直读）

`py`、`js`、`mjs`、`ts`、`tsx`、`jsx`、`vue`、`html`、`css`、`scss`、`sql`、`sh`、`bash`、`bat`、`ps1`、`java`、`go`、`rs`、`php`、`rb`、`swift`、`kt`、`c`、`h`、`cpp`、`hpp`、`cs`、`r`、`lua`、`pl`、`tex`

### 5.4 旧版与特殊 Office（只存不解析）

`doc`、`xls`、`ppt`、`rtf`、`odt`、`ods`、`odp`、`epub`

- 归入文档类，卡片上带提示：可下载后用 Office 或 WPS 打开，另存为 `docx/xlsx/pptx` 后可在线预览。
- 不做文本提取，预览按钮不会给出内容。

### 5.5 预览调用链

1. 文件卡片「预览」→ `GET /api/files/{fid}/text`。
2. 服务端按扩展名走 `extract_text()`，结果按 `fid + mtime` 缓存在进程内 `_TEXT_CACHE`。
3. 解析异常返回 `[解析失败：异常类型]`，不会 500。
4. 提取为空时返回原始上传时记录的 `note`（例如「未提取到文本，可下载查看」）。

## 6. 压缩包

支持扩展名：`zip`、`rar`、`7z`、`tar`、`gz`、`tgz`、`bz2`、`xz`

- 只在 `zip` 上做只读列目录：读 `namelist` 前 30 项，超出提示总数。**不解压**，避免 zip 炸弹与路径穿越。
- `rar`、`7z`、`tar`、`gz`、`tgz`、`bz2`、`xz` 仅存原文件，可下载。
- 压缩包内容不参与任何文本索引。

## 7. 安全校验与限制

### 7.1 扩展名与文件头一致性

`STRICT_FILE_MAGIC` 为 `1` 时（`SECURITY_LEVEL=public` 默认开启），对下表类型比对文件头；不匹配直接 `415` 拒收。

| 类型 | 校验规则 |
| --- | --- |
| `png` / `jpg` / `gif` / `bmp` / `tiff` / `ico` | 对应魔数（`\x89PNG`、`\xff\xd8\xff`、`GIF8`、`BM`、`II*\x00`/`MM\x00*`、ICO 头） |
| `webp` / `wav` / `aiff` | `RIFF`+`WEBP`、`RIFF`+`WAVE`、`FORM`+`AIFF` |
| `mp4` / `m4v` / `mov` / `3gp` | 第 4–8 字节为 `ftyp` |
| `mkv` / `webm` | EBML 头 `\x1aE\xdf\xa3` |
| `mp3` / `aac` / `amr` / `ogg` / `opus` / `flac` | `ID3`/帧同步、`\xff\xf1`/`\xff\xf9`、`#!AMR`、`OggS`、`fLaC` |
| `pdf` | `%PDF` |
| `doc` / `xls` / `ppt` | OLE 头 `\xd0\xcf\x11\xe0` |
| `docx` / `xlsx` / `pptx` / `odt` / `ods` / `odp` / `epub` / `zip` | `PK\x03\x04` |
| `rar` / `7z` / `gz` / `tgz` / `bz2` / `xz` | `Rar!`、`7z\xbc\xaf`、`\x1f\x8b`、`BZh`、`\xfd7zXZ` |
| `rtf` | `{\rtf` |

未列入上表的扩展名（纯文本、代码、`avif`、`jsonl` 等）不做文件头校验，一律放行。

### 7.2 危险内联类型强制下载

`html`、`htm`、`svg`、`xml`、`xhtml`、`js`、`mjs`、`css`：

- 下载接口一律 `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff`。
- 缩略图接口对非 `image/*` 的记录同样按 `application/octet-stream` 附件返回，避免把 HTML 当图片内联渲染。

### 7.3 大小与配额

| 项目 | 默认值 | 配置项 |
| --- | --- | --- |
| 单文件上限 | 300MB | `MAX_UPLOAD_MB` |
| 全站总配额 | 2048MB | `MAX_TOTAL_MB` |
| 管理员超限 | 可确认后继续上传（`X-Admin-Confirm: 1`） | `ADMIN_CONFIRM` |
| 普通用户总配额触顶 | `507` 服务器存储已满 | — |

### 7.4 上传接口形态

- 单请求：`POST /api/files`，请求体是**原始字节**，文件名与 MIME 走 `X-Filename`、`X-Mime` 头，鉴权走 `X-Device-Secret` 与 `?device=`。
- 分片：`POST /api/files/chunk/init` 拿 `up_id` → 逐片 `PUT /api/files/chunk/{up_id}/{idx}` → `POST /api/files/chunk/{up_id}/finish` 合并并落盘。
- 下载：`GET /api/files/{fid}`；原图直链：`GET /media/{fid}`；缩略图：`GET /thumb/{fid}?w=128`。

## 8. 客户端表现对照

| `kind` | 气泡渲染 | 额外能力 |
| --- | --- | --- |
| `image` | `<img src="/media/{fid}">` | 点开大图、设为表情、进编辑器（裁剪/旋转/滤镜/画质档） |
| `video` | `<video controls>` + 自定义全屏 | 下载 |
| `audio` | `<audio controls>` | 下载 |
| `doc` | 文件卡片 | 预览（提取文本）、下载 |
| `archive` | 文件卡片 | `zip` 预览目录、下载 |
| `other` | 文件卡片 | 下载 |

## 9. 已知限制

- 不做服务端转码：视频/音频能不能播由客户端解码能力决定。
- 旧版 Office 不解析，只能下载后自行转换。
- 文本预览只读前 2MB；表格预览有行列截断（xlsx 5 表 80 行，docx 表 60 行）。
- 扫描版 PDF 无文本层时预览为空，需要 OCR 得外部处理。
- `xlsm` 虽在提取函数的分支里，但不在 `DOC_EXT` 集合内，实际按「其他」处理，只能下载。
- 图片缩略图按需生成并缓存，源图更新（同 fid 不会更新）不会重建缓存。

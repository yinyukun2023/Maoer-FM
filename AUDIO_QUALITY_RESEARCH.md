# 猫耳 FM 音质来源研究

核对日期：2026-10-05。本文区分**当日实测**、**官方代码事实**和**尚未验证**。网页接口与安装包行为是当前实现，不是官方承诺稳定的开放 API。

**后续实施已完成**：应用户要求，正式播放和下载均自动选择官网返回的最高码率音轨，不增加音质选项；只有较低档位或单一档位时使用现有音轨。下载在 `MaoerApi.playback_info()` 中选档；播放在官网创建播放器之前筛选音轨，随后仍由官方播放器加载和授权。视频继续保存原 MP4。以下研究中的“第一条”描述修改前的行为。

正式流程复测：“突击采访”播放清单只加载了 357,202 bit/s 的高档，播放、暂停、恢复、跳转均成功；下载得到 44,133,630 字节、平均音频码率 355.438 kbps 的完整 M4A，其编码数据包哈希与前次研究导出的高档一致。见 [播放实测](build/audio_quality_probe/playback_13661353.json) 和 [正式下载实测](build/audio_quality_probe/production_13661353/result.json)。另已复测《蝉女》花絮下载，MP4 与官网响应逐字节一致。

实施验证：完整回归 511 项通过，语法检查通过；补充了最高档不按 ID 或列表顺序判断、单档、缺少可用高档地址、空音轨列表等离线检查。《蝉女》花絮的原视频播放、暂停、恢复和跳转也已通过，[记录](build/audio_quality_probe/playback_1052281.json)。本次修改源码，未重新打包 EXE。

**结论：同一集《灯花笑》上季“突击采访”确实可以从官网取得更高码率音轨，并已完整导出、独立解码成功。** 修改前下载代码选择第一条有效 DASH 音轨；研究阶段在测试脚本中选择返回列表内 `bandwidth` 最大的音轨，得到约 355.438 kbps 的 AAC，普通版本约 133.932 kbps。两份文件均保留服务端编码数据，没有重新编码。该样本没有视频来源，更高档来自 `dash.audio`，不需要视频模式。

## 同一集的两档实测

样本：[广播剧《灯花笑》突击采访](https://www.missevan.com/sound/13661353)，所属[《灯花笑》上季](https://www.missevan.com/mdrama/94733)。本次可匿名访问[官方声音详情接口](https://www.missevan.com/sound/getsound?soundid=13661353)，没有使用账号、购买或改变权限。

| 检查项 | 普通档 | 较高档 |
| --- | --- | --- |
| 接口顺序与 `id` | `dash.audio[0]`，128 | `dash.audio[1]`，64 |
| 接口 `bandwidth` | 135,695 bit/s | 357,202 bit/s |
| 接口源媒体大小 | 16,780,954 字节 | 44,168,191 字节 |
| 最终 M4A 大小 | 16,745,977 字节 | 44,133,630 字节 |
| 编码数据包计算的平均码率 | 133.932 kbps | 355.438 kbps |
| 实际编码 | AAC-LC，48,000 Hz，双声道 | AAC-LC，48,000 Hz，双声道 |
| 完整解码时长 | 989.126417 秒 | 989.126417 秒 |
| 音频数据包 / 独立解码帧数 | 46,366 / 46,366 | 46,366 / 46,366 |
| 不传密钥独立解码产物 | 通过 | 通过 |

**当日实测**：两条音轨地址不同，具有相同的密钥标识；两份导出的 AAC 配置一致，编码数据包内容不同。两条路径的原始编码包完整性校验均通过。两次下载均无系统音量修改、无残留 `.part`、无输出 `pssh`，临时播放配置目录已清理。脱敏记录见 [comparison.json](build/audio_quality_probe/comparison.json)、[普通档结果](build/audio_quality_probe/standard/result.json)、[较高档结果](build/audio_quality_probe/higher/result.json)、[较高档媒体检查](build/audio_quality_probe/higher_media.json)。

码率按音频数据包字节数、实际时长计算，不把容器开销、视频数据或接口字段名算成音频码率。`id=64` 在这里明显不是 64 kbps；也不能仅按 `id` 的数值大小排列音质。较高码率证明取得了另一条更高数据量的编码流，不等于证明无损、原始母带或手机当前播放字节完全相同。

## 修改前程序为何选到普通档

**修改前代码事实**：[maoer_api.py](maoer_api.py) 的 `MaoerApi.playback_info()` 将 `dash.audio` 中第一条具有 `base_url` 的记录存入 `PlaybackInfo.dash_audio`，没有比较 `bandwidth`。[downloads.py](downloads.py) 的 `download_audio()` 在无视频地址时直接下载这条记录。对上述样本，这条记录是 `id=128` 的普通档。

**研究阶段验证范围**：最初保持正式程序行为原样，仅在测试脚本中将目标音轨换成列表里带宽最大的那条，再完成整集下载、封装、包校验及独立解码；后续正式实施及复测见本文开头。

**已采用的选档规则**：按有效数值 `bandwidth` 选择最高档，保留权限检查；不硬编码“第二条”、`id=64`、文件后缀或固定 320 kbps。只有一档时仍下载已有档位。已有视频继续按用户要求保存原视频文件。

## 官网播放器的选择依据

2026-10-05 从[官网样本页](https://www.missevan.com/sound/13449615)核对当前脚本引用，并匿名获取下列官方静态资源。脚本副本仅放在忽略目录 `build/audio_quality_sources/`。

| 来源 | 已确认逻辑 | 不能据此认定 |
| --- | --- | --- |
| [官网播放器主脚本](https://s1.maoercdn.com/assets/www/js/dist/bundle.d12bfedc.js)，`createPlayer`，约第 93833 行 | 非 iframe、有 `videourl` 且未选 `sound-mode` 时使用视频地址；音频模式使用 `soundurl`，DRM 音频还传入 `sound.dash`。 | 视频地址不保证含更高质量音轨。 |
| 同一主脚本，`DashLoader`，约第 28344 行 | 接收 `dash.audio`，未显式设置更高的默认音频档位。内嵌 `DashPlayer` 默认 `defaultAudioQuality=0`、音频自动换档关闭；清单条目按 `bandwidth` 升序建立索引。 | 这是静态默认逻辑，不代表所有浏览器、所有运行时路径都已经逐一测量。 |
| [官网页面操作脚本](https://s1.maoercdn.com/www/public/20260917/js/index.min.js) | `.btn-mode-switcher` 切换 `sound-mode` / `video-mode` 后重载当前节目；也保存 `soundurl_32`、`soundurl_64`、`soundurl_128` 字段。 | 保留这些字段不证明当前播放器存在对应音质菜单，更不证明后缀等于实际码率。 |
| [手机网页版脚本](https://s1.hdslb.com/bfs/static/maoer-static/assets/m/js/mobile.811cc16d.js) | 声音详情请求使用 `/sound/getsound?soundid=<id>`；视频分支将 `videourl` 赋给视频元素，音频分支使用 `soundurl`。 | 手机网页版与 Android 原生 App 不能混为同一播放实现。 |

**当日代码观察**：上述实际声音详情请求不需要额外音质参数就返回两条 DASH 音轨。本次没有找到可作为依据的官网 `quality=` 等“隐藏高音质参数”，无需靠猜测请求参数获取已返回的较高档。内嵌通用播放器支持的其他编码或档位，也不能直接当成猫耳已经向本节目提供的媒体。

主脚本 SHA-256：`e324ef651de63e24cc8577b5334da70e5c853e80d5de4ebface7b8bc6fcaf8e7`；页面操作脚本：`2cb5a35d31e0c449c44a0940f78b0f0291018464e331c6b0ad5077a5fbdaffaf`；手机网页版脚本：`f7669dfb70d4ce932a86cc409ef9525cb39995a13efade40f4368a8a1c7f7ddf`。脚本定位、默认值及函数名只针对这些版本。

## “通过视频获得更高音质”的适用范围

**当日实测**：“突击采访”返回 `videourl` 为空、`dash.video=[]`，较高档仍然存在于 `dash.audio`。因此该节目可以直接取较高音频档，不需要寻找视频地址。

其他匿名样本的[脱敏接口抽样](build/audio_quality_probe/baseline.json)进一步说明，档位名称不能代替测量：

| 官方节目 | `id=128` 的 `bandwidth` | `id=64` 的 `bandwidth` |
| --- | ---: | ---: |
| [《触礁》第一集，13449615](https://www.missevan.com/sound/getsound?soundid=13449615) | 131,758 | 344,459 |
| [《蝉女》第一期，1048742](https://www.missevan.com/sound/getsound?soundid=1048742) | 132,421 | 327,028 |
| [《蝉女》花絮，1052281](https://www.missevan.com/sound/getsound?soundid=1052281) | 136,562 | 136,690 |
| [《蝉女》花絮1，1052323](https://www.missevan.com/sound/getsound?soundid=1052323) | 141,052 | 140,966 |
| [《蝉女》第九期预告，1082628](https://www.missevan.com/sound/getsound?soundid=1082628) | 130,209 | 477,714 |

单位均为 bit/s，以上表格是接口标注，只有“突击采访”两档完成本轮整集导出对照。尤其 `1052323` 的 `id=64` 带宽略低，不能统一把该 ID 当成更高档。

已有两份《蝉女》花絮原始 MP4 的音频数据包实测分别约 106.576、107.850 kbps；视频文件虽更大，音频并不因此更高。来源、解码、原文件保存验证见 [DOWNLOAD_RESEARCH.md](DOWNLOAD_RESEARCH.md)，本次音频测量见 [baseline.json](build/audio_quality_probe/baseline.json)。

## Android“原音音质”：已知与未知

**官方安装包事实**：从[官网 App 下载页](https://www.missevan.com/app)的[Android 下载入口](https://www.missevan.com/app/download?device=0&ct=missevan)获取[官方 APK](https://static.maoercdn.com/app/202609/23/MissEvan.apk)，核对为 `cn.missevan` 6.7.7，版本号 6070720，与本次已连接手机上的版本号相同。使用本机 Android SDK 的 `aapt` / `dexdump` 只读检查，未安装或修改 App。APK 为 161,692,293 字节，SHA-256：`15f1f36057733614b84955adb6bfbd45ebb1f340dcf9318f2d79b52a564a195e`。

**官方代码事实**：安装包有“原音音质”设置资源及“当前音频无其他音质版本”提示。`PlayController` 的声音 URL 选择方法调用 `GeneralKt.isOriginSoundOn()`：开启时优先 `SoundInfo.soundUrlList`，为空回落 `soundUrl128List`；关闭时顺序相反。`switchOriginalSoundQuality()` 按该规则取地址，再调用播放器的 `switchUrl()`。这表明该版本的原音开关会切换媒体来源。仅保留相关方法的静态证据于 [apk_quality_selection.txt](build/audio_quality_sources/apk_quality_selection.txt) 和 [apk_quality_methods.txt](build/audio_quality_sources/apk_quality_methods.txt)。

**尚未验证**：本轮未完成 `soundUrlList` 与网页 `dash.audio` 的跨端字节对应关系，也未通过手机播放切换实测原音档；依用户后续指示停止手机对照，网页较高档可获取已完成验证。[官方 App Store 说明](https://apps.apple.com/cn/app/id1148465254)中的“高清音质”没有给出完整编码、码率或无损规格，不能补足这些缺失证据。不宣称导出的较高档已与手机播放逐字节一致，也不把听感差异全部归因于已证实的选档差异。

本报告及脱敏结果不保存账号信息、Cookie、内容密钥或签名媒体 URL。公开脚本、APK、探针产物均放在忽略的 `build/` 目录。

## 视频画质与内嵌音轨复核（2026-10-05）

**本轮结论**：匿名抽样的四个视频节目均只返回一个 `videourl`，`dash.video` 为空；没有发现这些样本的多分辨率视频版本或官网清晰度选择入口。同时，接口确实另返两条 `dash.audio`，但它们是独立的纯音频版本，不能据此说 MP4 内有两条音轨。以下结论限于本轮样本和当前官网播放路径，不断言全站视频永远只有一档。

**官方代码事实**：重新获取当前[官网主脚本](https://s1.maoercdn.com/assets/www/js/dist/bundle.d12bfedc.js)和[页面操作脚本](https://s1.maoercdn.com/www/public/20260917/js/index.min.js)。主脚本模块 2269 的 `createPlayer` 在视频模式仅传入一个 `videourl`，DASH 数据只交给音频模式；模块 2023 的 `NativeLoader` 把这个地址直接设为媒体元素的 `src`。页面的模式按钮切换视频与音图，没有切换视频分辨率。主脚本里的 `setQualityFor` 全部位于通用 DASH 播放器库模块 2279，不能当成猫耳提供了视频画质菜单。两份脚本哈希与前文一致；取证时使用临时目录 `build/video_quality_sources/`，该目录随后已被外部清理，本文保留官方来源、版本定位及实测数字。

**当日媒体实测**：下列三个原始 MP4 均为 H.264 视频、AAC-LC 音频；音频均为 48 kHz、双声道，每个文件只有 **1 条视频流 + 1 条音频流**。通过解析文件内的流和编码数据包确认，没有从扩展名推断。

| 官网样本 | 视频分辨率 | 文件大小 | 时长 | MP4 内音频平均码率 |
| --- | --- | ---: | ---: | ---: |
| [《蝉女》花絮，1052281](https://www.missevan.com/sound/1052281) | 1920 × 1080 | 11,198,221 字节 | 91.477 秒 | 106.576 kbps |
| [《蝉女》花絮1，1052323](https://www.missevan.com/sound/1052323) | 1920 × 1080 | 10,814,411 字节 | 117.589 秒 | 107.850 kbps |
| [《蝉女》花絮2，1053073](https://www.missevan.com/sound/1053073) | 1920 × 1080 | 21,177,360 字节 | 132.629 秒 | 160.623 kbps |

这些节目的接口另外返回 `id=128`、`id=64` 两份音频，标注带宽都约为 135–141 kbps。补查[《太傅他人人喊打》视频花絮，9322186](https://www.missevan.com/sound/9322186)，同样只有一个视频地址，另附两份音频的带宽为 130,355、131,957 bit/s。四个样本的两条音频记录均有有效且彼此不同的 `base_url`。**两份音频不必然是一低一高、相差明显的音质档位**；独立音频也不保证比原 MP4 内的声音更高码率。本轮未下载补查样本的 MP4，也没有将四个样本推广为全站规格。媒体和记录曾暂存于 `build/video_quality_probe/`，随后已被外部清理；此类临时目录会随打包清理，不保证持续存在。

**当前程序行为保持不变**：视频仍播放、下载官网原始 MP4，使用其中的原有音轨；不会把另返的 `dash.audio` 自动合并进视频或替换视频声音。纯音频模式的最高码率选择与视频内嵌音轨是两条路径。本轮仅研究与记录，没有修改生产代码。

## 播放音质疑虑复核（2026-10-05）

用户反馈下载正常、播放音质不确定后，使用正式 `HiddenBrowserPlayer` 和独立的匿名 WebView2 配置重新实测。通过浏览器实际完成的媒体请求匹配接口音频地址，并检查播放位置持续前进；不只检查接口返回了哪些档位。三个样本均在首次播放、暂停恢复、跳转至 30 秒和重新加载后保持最高档，未复现播放选择低档的问题。

| 样本 | 接口普通档 / bit/s | 实际播放来源的标注带宽 / bit/s | 下载选档 |
| --- | ---: | ---: | --- |
| 《灯花笑》上季“突击采访”，13661353 | 135,695 | 357,202 | 同一高档 |
| 《蝉女》第一季第一期，1048742 | 132,421 | 327,028 | 同一高档 |
| 《触礁》第一集，13449615 | 131,758 | 344,459 | 同一高档 |

**负对照**：仅在临时探针里禁用最高音质脚本，再播放“突击采访”，实际请求变成 135,695 bit/s 的普通档；同一检查准确报告 `playing_lower_quality_than_download`。因此探针能够区分实际高低档，而不是仅凭脚本存在或播放成功判断。以上码率是已匹配媒体来源的接口标注，和前文按完整 AAC 数据包计算的平均码率口径不同。

现有 `tests/test_audio_quality.py` 的 3 项检查通过。另只读检查仓库内 `dist/完整包/Maoer-FM.exe`，内含最高音质脚本文本与当前源码一致；这不是对用户其他位置安装包的运行验证。本轮未改变播放或下载代码，也未重新打包。结论限于本轮样本，未做手机听感或所有节目、网络条件的对照。

复核探针及脱敏 JSON 暂存于忽略的 `build/playback_quality_verification/`；该目录可能随打包清理，上述核心结果保存在本文。不保留 Cookie、签名媒体地址、内容密钥或测试 WebView2 配置。

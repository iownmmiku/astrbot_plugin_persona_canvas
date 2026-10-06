# 人设影像工作室

AstrBot 插件：用自然语言调用 GPT/OpenAI、Gemini、NovelAI 或自定义生图接口，管理可持续的人设状态，并通过 WebUI 配置主动私聊消息与每日早安照片。

> 当前版本是可运行的首版原型。不同 Gemini、NovelAI 中转服务的参数和图片返回格式可能不同，正式使用前请在 WebUI 中先执行小图测试。

## 功能

- **自然语言生图**
  - “给我拍一张你的自拍”使用当前人设。
  - “换成白色连衣裙”“摆一个坐姿”更新服装和姿势等动态状态。
  - “给我画一片大海”作为普通场景生图，不加入人设。
- **完整 WebUI**
  - 总览、人设工作台、模型接口、生图工作台、主动消息、生成历史。
  - 人设支持正面提示词、负面提示词、画风、动态状态和早安随机服装池。
  - API Key 不回显，业务数据原子写入插件数据目录。
- **模型接口**
  - OpenAI/GPT Images 兼容接口。
  - Gemini `generateContent` 图像响应。
  - NovelAI 原生 `ai/generate-image` 请求。
  - 可配置认证头、认证前缀、模型和图生图能力声明。
- **参考图能力**
  - Provider 明确声明支持图生图时才发送参考图。
  - 不把视觉识别或文字重绘伪装成真正的图改图。
- **普通用户保护**
  - 本地硬性拦截规则。
  - 每日次数、最小间隔、并发限制。
  - 管理员免除本插件的普通用户配额，但不能绕过平台或服务商的安全边界。
- **主动 QQ 私聊**
  - 保存完整 `unified_msg_origin`，不只保存 QQ 号。
  - 可设置时间段、最小间隔、连续未回复次数和静默时长。
  - 收到用户消息后会解除静默并清零未回复计数。
- **每日早安**
  - 每个目标每天最多触发一次。
  - 在独立时间段内发送随机服装的人设照片和问候语。
  - 状态持久化，重启和重复轮询不会重复领取。

## 安装

### 方式一：下载发行版

从 GitHub Releases 下载 `astrbot_plugin_persona_studio-v*.zip`，解压后将其中的 `astrbot_plugin_persona_studio` 目录复制到：

```text
AstrBot/data/plugins/astrbot_plugin_persona_studio/
```

然后在 AstrBot WebUI 中启用插件并重载。

### 方式二：克隆仓库

```bash
git clone https://github.com/iownmmiku/astrbot_plugin_persona_studio.git
```

将仓库目录放入 `AstrBot/data/plugins/`，目录名保持为 `astrbot_plugin_persona_studio`。

插件依赖 AstrBot 已提供的 Python 环境和 `aiohttp`，不需要 Node.js 或 npm 构建 WebUI。

## 首次配置

1. 在 AstrBot 插件配置中开启插件和 WebUI。
2. 默认监听 `127.0.0.1:3018`。
3. 管理员在 QQ 中发送：

   ```text
   /生图控制台
   ```

4. 打开返回的 WebUI 地址，输入访问令牌。
5. 先进入“模型接口”配置 Provider，再到“人设工作台”填写固定外观和提示词。
6. 在“生图工作台”先测试一张小图。

如果 `webui_token` 留空，令牌会自动保存到插件数据目录的 `webui_token.txt`。令牌不会写入日志；请不要把它提交到 Git 或公开截图中。

## 使用

聊天侧只保留两个入口：

```text
/生图控制台
/生图 给我画一片大海
```

正常情况下直接说自然语言即可。插件会先判断是否为生图请求，普通聊天不会触发生图。

示例：

```text
给我拍一张你的自拍
换成白色连衣裙，坐在窗边
给我画一片大海，夕阳，电影感
```

## WebUI 配置说明

### 人设工作台

固定人设和动态状态分开保存：

- 固定：画风、长期外观、正面提示词、负面提示词。
- 动态：服装、姿势、表情、场景。
- 早安服装池：每行一个随机服装描述。

“换衣服”和“改姿势”只更新动态状态，不会覆盖固定外观。

### 模型接口

| 类型 | 典型接口 | 注意事项 |
| --- | --- | --- |
| GPT/OpenAI | `/v1/images/generations` | 需要返回 `data[0].b64_json`，编辑能力需服务端支持 `/v1/images/edits` |
| Gemini | `models/{model}:generateContent` | 需要返回 `candidates[].content.parts[].inlineData.data` |
| NovelAI | `/ai/generate-image` | 使用 NovelAI 原生 JSON；V4/V5 字段取决于模型和中转服务 |

接口地址、模型名称和返回格式由实际服务商决定。插件默认不下载第三方返回的图片 URL，以避免不受控的二次请求；推荐配置 base64 或直接图片响应。

### 主动消息

在 QQ 私聊中先和机器人产生一条消息，目标才会出现在 WebUI 的“主动消息”页。管理员可以在那里启用目标、设置普通主动消息和早安消息。

主动消息发送依赖 AstrBot 平台适配器能力，QQ 官方平台可能不支持所有主动发送场景。

## 数据文件

插件数据默认位于 AstrBot 插件数据目录：

```text
personas.json       人设档案
settings.json       Provider、审核和主动消息设置
targets.json        私聊目标与主动状态
history.jsonl       生成记录和错误摘要
runtime.json        运行时版本与早安状态
webui_token.txt     自动生成的 WebUI 令牌
assets/             参考图和生成图片
```

请备份这些文件时注意其中可能包含私聊目标信息和人设提示词。API Key 保存在 `settings.json` 中，部署时应限制数据目录权限，不要把运行数据提交到公开仓库。

## 安全边界

- WebUI 默认只监听本机。需要远程访问时，建议使用 HTTPS 反向代理、访问控制和防火墙，不要直接暴露到公网。
- 管理员的“无限制”仅指不受本插件普通用户配额影响，不代表可以绕过模型服务商、平台规则或法律要求。
- 生成请求会经过本地硬规则；高风险内容会被拒绝。
- 日志不应记录完整私聊内容、API Key 或图片二进制。

## 开发与检查

```bash
py -m py_compile storage.py intent.py moderation.py providers/base.py webui_server.py active.py main.py tests/test_core.py
```

核心冒烟测试：

```bash
py - <<'PY'
import sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")
from intent import heuristic_intent
from storage import Storage
assert heuristic_intent("给我画一片大海").mode == "scene"
with tempfile.TemporaryDirectory() as d:
    Storage(Path(d))
print("SMOKE_OK")
PY
```

真实 GPT、Gemini、NovelAI 接口不包含在自动测试中，请使用 WebUI 的小图测试验证具体服务商配置。

## 许可证

暂未指定开源许可证。公开使用前请根据你的发行计划补充许可证文件。

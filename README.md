# 🧹 TG Intermediate Cleanup

**中文** | [English](#english)

让 QwenPaw 的 Telegram 聊天窗口自动变干净 ✨

机器人干活的时候,聊天窗口会蹦出一堆过程消息:💭 思考片段、工具调用、工具结果、中间生成的图片/卡片……等最终答案一发出,这个插件就把它们**全部删掉**,聊天里只留干货。

- 🍃 删完只剩最终答案(答案带的配图/文件会保留)
- 🛡️ 出错或取消时**不动手**,保留现场方便排障
- 🔧 纯运行时 monkey-patch,**不改框架源码**,QwenPaw 升级不受影响
- ♻️ 装卸幂等,启动时装、关机时还原

## 🎬 效果

```
删除前 😵                          删除后 🍃
┌──────────────────────┐          ┌──────────────────────┐
│ 💭 正在思考...        │          │                      │
│ 🔧 调用工具 get_time  │          │  最终答案(一条)       │
│ 📄 工具结果: {...}    │   ──→    │  (配图也会保留)       │
│ 🖼️ [中间图片]         │          │                      │
│ ✅ 最终答案           │          └──────────────────────┘
└──────────────────────┘
```

## 📦 安装

把整个目录放进 QwenPaw 的插件目录(persistent 卷),然后**重启 host**:

```bash
cp -r tg-intermediate-cleanup <你的插件目录>/plugins/   # Docker 部署通常是 /app/working/plugins
supervisorctl restart app
```

重启后看日志确认插件生效:

```
tg_cleanup: patches installed (v1.0.0)
```

> ⚠️ 别用 `qwenpaw plugin install` 热装 —— 会触发 host channel 重载,导致 Telegram 双 polling 实例抢 `getUpdates`(HTTP 409 Conflict),必须重启 host 才干净。

## ⚙️ 它是怎么工作的

Telegram channel 用占位消息(原地编辑)和普通 `send()` 展示流式进度。插件在启动钩子里对 `TelegramChannel` / `Bot` 做运行时补丁:

1. **追踪**:一个请求产生的每条消息都记下 message id,按"单元"分组(文本+配图+卡片算一个单元),并标记类型(`answer-stream` / `message` / `process-stream` / `notice`)
2. **清理**:请求正常结束时(`_on_process_completed`),删掉除"最后一个答案单元及其附属"以外的所有消息
3. **兜底**:删除失败自动重试一次(处理 `RetryAfter`);还不行就把消息编辑成 `…` 折叠;消息本来就不在了不算失败

## 🛡️ 安全设计

- **宁可不清,绝不误删**:找不到可识别的答案单元 → 一条都不删 + 打 WARNING(框架以后改内部结构,最坏情况是"没清理",永远不会"删掉答案")
- **出错保留现场**:异常路径走不到清理钩子,中间消息原样保留,方便排障
- **安装预检**:7 个补丁目标全部存在才动手,中途失败自动回滚,不会停在"半补丁"状态

## ❓ FAQ

**Q: 出错的时候也会删消息吗?**
不会。清理钩子只挂在"正常完成"路径上,报错/取消时保留现场。

**Q: 会删掉最终答案吗?**
不会。答案单元(含配图)始终保留;这是插件的最高优先级。

**Q: 群里没有删消息权限会怎样?**
删除失败会降级成"编辑成 …",消息内容被折叠。

**Q: 环境要求?**
QwenPaw 2.2.x(≥ 2.2.0,< 2.3.0),Python 3.10+(只在 3.11 实测)。

## 🧪 测试

自带一个仿真测试架,模拟框架生命周期跑 6 个场景(流式答案、媒体保留、失效保护、错误路径、真实装卸幂等):

```bash
python3 tests/sim_tgc.py
```

## 📄 License

[MIT](LICENSE)

---

<a id="english"></a>
# 🧹 TG Intermediate Cleanup (English)

Keep your QwenPaw Telegram chats clean automatically ✨

While the bot works, a bunch of progress messages pile up in the chat: 💭 thinking snippets, tool calls, tool results, intermediate images/cards... Once the final answer is sent, this plugin **deletes them all**, leaving only the good stuff.

- 🍃 Only the final answer remains (media attached to the answer is kept)
- 🛡️ Never touches anything on error/cancel — the scene is preserved for debugging
- 🔧 Pure runtime monkey-patch, **no framework source edits** — survives QwenPaw upgrades
- ♻️ Idempotent install/uninstall (applied at startup, restored at shutdown)

## 🎬 Effect

```
Before 😵                          After 🍃
┌──────────────────────┐          ┌──────────────────────┐
│ 💭 thinking...        │          │                      │
│ 🔧 tool: get_time     │          │  final answer (1)    │
│ 📄 result: {...}      │   ──→    │  (media kept)        │
│ 🖼️ [intermediate img] │          │                      │
│ ✅ final answer       │          └──────────────────────┘
└──────────────────────┘
```

## 📦 Install

Drop the folder into QwenPaw's plugin directory (persistent volume), then **restart the host**:

```bash
cp -r tg-intermediate-cleanup <你的插件目录>/plugins/   # Docker 部署通常是 /app/working/plugins
supervisorctl restart app
```

Check the log to confirm:

```
tg_cleanup: patches installed (v1.0.0)
```

> ⚠️ Do NOT use `qwenpaw plugin install` hot-install — it reloads the channel and causes dual polling instances fighting over `getUpdates` (HTTP 409 Conflict). A host restart is the clean way.

## ⚙️ How it works

The Telegram channel shows streaming progress via placeholder messages (edited in place) and plain `send()` calls. The plugin patches `TelegramChannel` / `Bot` at runtime in a startup hook:

1. **Track**: every message produced by one request is recorded with a message id, grouped into "units" (text + media + card = one unit), and typed (`answer-stream` / `message` / `process-stream` / `notice`)
2. **Clean**: when the request completes normally (`_on_process_completed`), everything except the last answer-like unit (and anything appended after it) is deleted
3. **Fallback**: a failed delete honours `RetryAfter` and retries once; if it still fails (e.g. no admin rights in a group) the message is collapsed via `edit_message_text("…")`; already-gone messages don't count as failures

## 🛡️ Safety design

- **Better safe than sorry**: if no answer-like unit can be identified, NOTHING is deleted and a WARNING is logged. If the framework layout changes in the future, the worst case is "no cleanup" — never "the answer got deleted"
- **Errors preserve the scene**: the cleanup hook only fires on the success path
- **Install pre-flight**: all 7 patch targets are verified before patching; a mid-install failure rolls everything back, so the plugin can never sit half-patched

## ❓ FAQ

**Q: Does it delete messages on error?**
No. Cleanup only runs on the normal-completion path. Errors and cancellations keep the scene.

**Q: Can it delete the final answer?**
No. The answer unit (with its media) is always kept — that's the plugin's top priority.

**Q: What if the bot has no delete permission in a group?**
Failed deletes fall back to collapsing the message into "…".

**Q: Requirements?**
QwenPaw 2.2.x (>= 2.2.0, < 2.3.0), Python 3.10+ (tested on 3.11 only).

## 🧪 Tests

A simulation harness is included. It replays 6 scenarios against the patched logic (streaming answer, media retention, fail-safe, error path, real install/uninstall idempotency):

```bash
python3 tests/sim_tgc.py
```

## 📄 License

[MIT](LICENSE)

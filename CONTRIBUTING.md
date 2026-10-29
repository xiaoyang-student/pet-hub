# 参与贡献 · Contributing

感谢你愿意花时间让 PetHub 变得更好。**任何形式的参与都算贡献**：报一个 bug、提一个想法、
推荐一款该被跟踪的软件、改一句文案、写一个适配器，或者只是把它分享给朋友。

> 英文说明见文末 [English](#english)。

---

## 最快的方式：不写代码也能帮忙

| 想做什么 | 去哪里 |
|---|---|
| 用着不顺、有 bug | [Issues → Bug 反馈](.github/ISSUE_TEMPLATE/bug_report.yml) |
| 有个想法/建议 | [Issues → 功能建议](.github/ISSUE_TEMPLATE/feature_request.yml) |
| 想让某款 AI 软件也被跟踪 | [Issues → 推荐新软件](.github/ISSUE_TEMPLATE/new-app.yml) |
| 随便聊聊、晒桌面、提问题 | [Discussions](https://github.com/xiaoyang-student/pet-hub/discussions) |
| 觉得好用 | 点右上角 **Star** ⭐，并把它分享给可能喜欢的朋友 |

---

## 提交代码：分支与审核流程

为了让主分支始终保持可用，代码贡献走**两段式**：

```
main      稳定发布分支（我直接维护，带版本标签）
contrib   社区贡献分支（你的 PR 请发到这里）
```

1. **Fork** 本仓库；
2. 基于 **`contrib`** 分支新建你的分支，例如 `feat/track-cursor`；
3. 改完提交 **Pull Request**，base 选 **`contrib`**（不是 `main`）；
4. 我会审阅 —— 通过的代码合并进 `contrib`，再经测试后合入 `main` 并发布版本。

> 没被合并也完全正常：可能是与整体方向不一致、时机未到，或需要再打磨。
> 我会在 PR 里说明原因，不会静默关闭。

### 审核时会看什么

- **是否真的解决了问题** —— 附上"改之前 / 改之后"的说明或截图最有说服力；
- **不猜、不编** —— PetHub 的核心原则是宠物说的话必须来自应用自己上报的数据，
  不接受"看起来差不多"的模拟或占位内容；
- **不要写死路径** —— 安装目录、软件位置一律运行时探测（见 `agent-trace/core/discover.py`），
  不要出现任何具体盘符或用户名；
- **有测试** —— 改动 `agent-trace/` 请在 `tools/` 下补测试，跑 `python -m unittest discover -s .`；
- **改动可控** —— 一个 PR 一件事，别顺手重排整个文件。

### 本地跑起来

```bat
start.bat                          :: 启动（桌宠 + 桥接服务 + 追踪器）
```

```bash
cd agent-trace && python -m unittest discover -s .   # 追踪器测试（纯标准库，无需装依赖）
cd .. && python bridge/test_server.py                # 桥接服务测试
```

---

## 新增一款被跟踪的软件

这是最受欢迎的贡献之一。步骤：

1. 开一个 [推荐新软件](.github/ISSUE_TEMPLATE/new-app.yml) issue，把它的**数据存放位置**贴出来
   （通常在 `%APPDATA%`、`~/.xxx` 或安装目录的 logs 下）；
2. 我确认可行后，你在 `agent-trace/adapters/` 下加适配器，在 `core/discover.py` 的 `SPECS` 里登记；
3. 补一个 `tools/test_xxx.py`，并确认"抹掉安装目录信息后仍能读到数据"。

---

## 行为准则

参与即表示同意 [行为准则](CODE_OF_CONDUCT.md)：友善、耐心、就事论事。

---

## English

Thanks for helping — **any kind of help counts**: filing a bug, suggesting an idea, recommending an
app that should be tracked, fixing a typo, writing an adapter, or simply sharing the project.

- **Code**: fork → branch off **`contrib`** → open a PR against **`contrib`** (not `main`).
  I review everything there; accepted code is merged to `contrib`, tested, then released on `main`.
- **Review criteria**: solves a real problem; keeps the "the pet never guesses" rule (everything it
  says comes from data the apps actually report); no hardcoded paths or drive letters; tests included;
  one PR per topic.
- **Run locally**: `start.bat`; tests via `python -m unittest discover -s .` inside `agent-trace/`.
- Not merged doesn't mean not appreciated — I always explain why in the PR.

Please also read the [Code of Conduct](CODE_OF_CONDUCT.md).

# 协作开发规则

> **本文档是给 AI 编程助手（如 DeepSeek）阅读和遵循的协作规则。**
> 把它作为项目上下文 / 系统提示喂给 AI，让 AI 在改动本仓库时严格按以下规则操作。
> 规则以「可执行、不二义」为目标：能照抄的命令就照抄，需要判断的地方给了明确判定条件。

- 仓库：`astrbot_plugin_chat_echo`（AstrBot 插件）
- 维护者：2 人小团队（`AMYdd00`、`Yao-lin101`）
- 主仓库：https://github.com/AMYdd00/astrbot_plugin_chat_echo

---

## 0. 最高优先级规则（违反任何一条都算错误）

1. **任何改动开始前，先同步最新 `main`**：`git checkout main && git pull origin main`。基于旧代码开发是冲突和「误改回别人代码」的头号原因。
2. **不使用 `dev` 或任何长期分支。** 不要创建、不要维护、不要往 `dev` 提交。需要分支时，从最新 `main` 临时切出，合并后立即删除。（原因见 [第 2 节](#2-分支模型不要再用-dev)）
3. **绝不回退、覆盖、或「改回」别人已经合并进 `main` 的代码。** 若怀疑已合并的改动有问题，停下来在 PR / 群里说明，不要默默改回去。
4. **删除或重命名任何函数 / 接口 / 字段前，必须全局搜索所有调用方并一并修改**：`grep -rn "名称" .`。前后端、调用方与定义必须保持一致。
5. **推送前必须本地验证通过**（编译 + 实际功能验证，见 [第 5 节](#5-推送前必须执行的自检)）。

---

## 1. 两种工作方式：什么时候直接推 main，什么时候开 PR

这是 2 人小仓库，允许小修复直接推 `main`，但要按下面的判定来选路径。

### 路径 A —— 直接提交到 `main`（仅限「小修复」）

**同时满足以下全部条件**，才算「小修复」，可直接推 `main`：

- 改动集中在 1～2 个文件，且不超过约几十行；
- **不**改动公共接口、函数签名、配置 schema（`_conf_schema.json`）、数据库结构；
- **不**改动对方当前正在做的模块（不确定就先问 / 改走路径 B）；
- 本地已编译 + 验证通过。

操作：

```bash
git checkout main
git pull --rebase origin main        # 先拉最新，避免冲突
# ...改代码...
python -m py_compile <改动的.py>      # 验证
git add <文件> && git commit -m "fix: 简短描述"
git pull --rebase origin main        # 推送前再同步一次
git push origin main
```

### 路径 B —— 开分支 + PR（其余所有情况）

**只要不满足「小修复」全部条件，就走这里**：新功能、重构、改接口 / schema / 数据结构、改动较大、或可能和对方冲突的改动。

```bash
git checkout main && git pull origin main
git checkout -b feat/简短英文描述         # 见第 3 节命名规范
# ...改代码 + 多次小步提交...
python -m py_compile <改动的.py>
git push -u origin feat/简短英文描述
# 然后在 GitHub 上对着 main 发起 PR，写清：改了什么、为什么、怎么验证的
```

PR 合并后清理分支：

```bash
git checkout main && git pull origin main
git branch -d feat/简短英文描述
git push origin --delete feat/简短英文描述
```

> **拿不准走 A 还是 B？一律走 B（开 PR）。** 多一次 PR 的成本，远小于直接污染 `main` 的成本。

---

## 2. 分支模型：不要再用 `dev`

**结论：废弃 `dev`，只保留 `main` 一条长期分支。**

之前的流程用一个长期 `dev` 分支做开发，导致「每次 `main` 有小更新都要回头同步 `dev`」，非常麻烦且容易出错。正确做法是：

- **`main` 是唯一长期分支**，永远保持可用。
- 需要开 PR 时，**临时**从最新 `main` 切分支，做完合并、**立即删除**。临时分支生命周期短（几小时到几天），永远不需要长期维护，自然也就没有「同步 dev」的负担。
- **不要**在 `main` 和任何分支之间来回对穿合并（`main`→`dev`→`main`），这会把提交历史搅乱。改动只能单向流回 `main`。

迁移动作（一次性，由人来确认）：确认 `dev` 上没有「未合并进 `main` 的有用改动」后，删除 `dev` 分支；今后不再创建。

---

## 3. 提交信息与分支命名

### Commit 信息：`类型: 简短描述`

```
feat: 新增关键词触发开关
fix: 修复缓存页面统计数据不显示
docs: 补充协作规则文档
refactor: 拆分 web_api 路由注册
```

- 常用类型：`feat`/`fix`/`docs`/`refactor`/`style`/`perf`/`chore`。
- ❌ 禁止：`更新`、`改了点东西`、`111` 这类无意义信息。
- ❌ **禁止在 commit 信息开头带 ` ``` ` 反引号**（这是从代码框 / AI 输出里复制时带进来的，历史里已有多条，不要再出现）。

### 分支命名：`类型/简短英文描述`

`feat/keyword-trigger`、`fix/sqlite-lock`、`refactor/web-api`、`docs/contributing`。

---

## 4. 同步 main 与解决冲突

直接推 `main` 时（路径 A）：推送前永远先 `git pull --rebase origin main`。

功能分支落后于 `main` 需要更新时（路径 B）：

```bash
git fetch origin
git rebase origin/main
# 如有冲突，解决后：
git add <冲突文件> && git rebase --continue
git push --force-with-lease origin <你的分支>    # 仅对自己的临时分支使用，安全
```

冲突处理要点：
- 打开冲突文件，处理 `<<<<<<<` / `=======` / `>>>>>>>` 之间的内容，融合两边的合理改动。
- **必须删除全部冲突标记行**；提交前搜索确认无残留（残留会导致代码无法运行）。

---

## 5. 推送前必须执行的自检

每次推送（无论路径 A 还是 B）前，逐项确认：

- [ ] **能编译**：`python -m py_compile $(git diff --name-only main | grep '\.py$')`
- [ ] **功能已实际验证**：改了 WebUI（`pages/admin/index.html`）→ 打开管理面板点一遍对应标签页；改了逻辑 → 在 AstrBot 里实测对应场景。
- [ ] **关联处已同步**：改了函数 / 接口 / 字段名，调用方（含前端 JS）已一并改。
      —— 真实事故：前端调用 `config_helper.persona_replies()`，但后端从未定义该方法，导致「人设定制」读不出配置。
- [ ] **配置与管理页面已同步**：若修改了 `_conf_schema.json` 中的配置项，必须在 `pages/admin/index.html` 以及 `pages/admin/js/config.js` 中同步添加/修改，确保管理面板能正常显示、加载和保存。
- [ ] **未夹带**无关文件、调试 `print`、临时代码。
- [ ] **若发版**：版本号三处同步（见第 6 节）。

---

## 6. 发版：版本号三处必须同步

发布新版本时，以下三处版本号**必须一起改**，漏一处就会对不上：

1. `metadata.yaml` 的 `version`
2. `main.py` 中 `@register(...)` 的最后一个参数
3. `CHANGELOG.md` 顶部新增本次变更条目（按 `新增 / 修复 / 优化` 分组）

> 真实事故：`metadata.yaml` 已是 `1.2.1`，但 `main.py` 的 `@register` 还停在 `1.2.0` —— 发版漏改一处。

---

## 7. 禁止清单（高频翻车点，直接对照）

| ❌ 禁止 | ✅ 正确做法 |
|---|---|
| 基于旧代码开干 | 开工前先 `git pull origin main` |
| 维护 / 提交到 `dev` 等长期分支 | 只用 `main` + 临时分支 |
| 改回 / 回退别人已合并的改动 | 先沟通，不擅自改回 |
| 删 / 改函数却漏改调用方 | `grep -rn` 找全调用方一起改 |
| 配置项变更漏改前端管理页面 | 同步修改 `_conf_schema.json`、`pages/admin/index.html` 以及 `js/config.js` |
| 只改一处版本号 | 三处同步（第 6 节） |
| commit 信息带 ` ``` ` 或写「更新」 | `类型: 简短描述` |
| 冲突标记残留就提交 | 提交前搜索确认无 `<<<<<<<` |
| 拿不准还直接推 main | 拿不准就开 PR |

---

## 8. 命令速查

```bash
# 小修复直接推 main（路径 A）
git checkout main && git pull --rebase origin main
# ...改 + 验证...
git add <文件> && git commit -m "fix: ..."
git pull --rebase origin main && git push origin main

# 开 PR（路径 B）
git checkout main && git pull origin main
git checkout -b feat/xxx
# ...改 + 验证...
git push -u origin feat/xxx          # 再去 GitHub 发 PR 到 main

# 合并后清理临时分支
git checkout main && git pull origin main
git branch -d feat/xxx && git push origin --delete feat/xxx

# 后悔药（未 push 时）
git restore <文件>          # 丢弃未提交改动
git reset --soft HEAD~1     # 撤销上次 commit 但保留改动
```

> 任何命令不确定后果，**先不要执行，问人**。只要还没 `push`，几乎都能救回来。

# 发布流程（RELEASE.md）

本仓库的发布路径：**npm 发包 → GitHub 打 tag/Release → 向 awesome-dsh-plugin 提目录 PR**。
三步都做过一遍，下面按实际踩到的坑写。

首次发布：`@bitqyh/dsh-codex-bridge@0.1.0`，2026-11-04。

---

## 零、发版前自检

```bash
cd ~/Projects/gptwork/dsh-codex-bridge
git status                    # 必须干净
npm test                      # 必须全过（当前 201 项）
npm pack --dry-run            # 看清单：不该有 __pycache__ / .bak / 本机路径
```

`npm pack --dry-run` 要盯三件事：

- 文件名是 `bitqyh-dsh-codex-bridge-X.Y.Z.tgz`（scoped 包会带前缀）
- 没有 `assets/tools/__pycache__/*.pyc`（`.gitignore` 挡不住 npm 的 `files` 白名单，
  所以 `files` 里是 `assets/tools/*.py` 而不是 `assets/**/*`）
- 没有密钥、没有 `/Users/xxx` 绝对路径

> **教训**：`tests/` 曾经把一个**真实的** ai-memory auth token 写死在测试里。
> 每次发版前都要扫一遍：
> ```bash
> grep -rniE "AI_MEMORY_AUTH_TOKEN=[0-9a-f]{8,}|gho_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}" \
>   --include="*.py" --include="*.md" --include="*.js" . | grep -v node_modules
> ```
> 测试里的假凭据要写成一眼能看出来的合成值，比如 `FAKE-TEST-TOKEN-0000…`。

---

## 一、npm 发布

### 1.1 前提：scope 必须是一个「组织」

npm 的规则是 **组织名 = scope 名**。`@bitqyh/dsh-codex-bridge` 要能发，
必须先在 npm 上有一个叫 `bitqyh` 的组织（`@用户名` 也一样，不是免费的）。

建组织：npmjs.com → 头像 → **Add an Organization** → 名字填 `bitqyh` →
选 **Unlimited public packages**（免费）→ Create。

验证（先把 token 放进环境变量，不要直接写进命令行历史）：

```bash
read -rs NPM_TOKEN && export NPM_TOKEN      # 粘贴 token 后回车，不回显

curl -s -H "Authorization: Bearer $NPM_TOKEN" \
  https://registry.npmjs.org/-/org/bitqyh/user
# 期望：{"yydsqyh":"owner"}   ← 说明组织已存在且你是 owner
# 若返回 {"error":"Scope not found"}：组织还没建好
# 若返回 {"error":"You may not perform that action from your current IP."}：token 绑了 IP 白名单
```

### 1.2 需要的 token 类型

**`npm login` 拿到的 session token 不能发布。** 发版要建 **Granular Access Token**：

https://www.npmjs.com/settings/yydsqyh/tokens → Generate New Token → Granular

| 字段 | 必须 |
|---|---|
| Expiration | 短（1 天），发完就删 |
| Permissions | `Read and write` → **Publish and stage**（不是 stage only） |
| **Bypass 2FA for publish** | ✅ **勾上** |
| Packages and scopes | `All packages`（选具体包容易漏） |
| **IP ranges (CIDR)** | **留空** |

> **两个坑都是实际踩过的：**
>
> **① IP ranges 填了就发不出去。** 填了 CIDR 后换个出口 IP 就被拒，报
> `{"error":"You may not perform that action from your current IP."}`。
> 走代理时出口 IP 会变，一律留空。
>
> **② 「stage only」不能用。** 它能上传但不直接上线，`npm publish` 会报
> `E_STAGE_REQUIRED`，还必须维护者带 2FA 审批。要选「publish and stage」。

### 1.3 发布

```bash
cd ~/Projects/gptwork/dsh-codex-bridge
npm publish --//registry.npmjs.org/:_authToken=npm_xxxxx
```

### 1.4 关键：202 不等于立刻能装

成功时看到的是：

```
npm notice Publishing to https://registry.npmjs.org/ with tag latest and public access
npm notice Your package is being processed and may take a few minutes to become available.
+ @bitqyh/dsh-codex-bridge@0.1.0
```

**这时候 `npm view` 和 registry 可能还是 404。** 实测从 202 到可读用了
**100 秒**（连续 9 次 404 之后才 200）。不要以为失败了去重发。

确认真实状态的唯一可靠办法是看 npm 的调试日志：

```bash
grep "http fetch PUT" $(ls -t ~/.npm/_logs/*.log | head -1)
# 成功：http fetch PUT 202 https://registry.npmjs.org/@bitqyh%2fdsh-codex-bridge 2338ms
# 失败：http fetch PUT 404 …（404 而不是 403 是 npm 的惯用做法，见下）
```

轮询等待：

```bash
for i in $(seq 1 20); do
  sleep 10
  r=$(curl -s -o /dev/null -w "%{http_code}" \
    "https://registry.npmjs.org/@bitqyh%2Fdsh-codex-bridge")
  echo "$i: $r"; [ "$r" = "200" ] && break
done
```

### 1.5 报错对照表

| 现象 | 真实原因 | 处理 |
|---|---|---|
| `PUT … 404` | **scope 组织不存在**（npm 用 404 掩盖，不报 403） | 先建组织（1.1） |
| `403 … Two-factor authentication or granular access token with bypass 2fa enabled` | token 没勾 Bypass 2FA，或用了 session token | 重建 token（1.2） |
| `E_STAGE_REQUIRED` | token 是 stage-only | 改选 publish and stage |
| `You may not perform that action from your current IP` | token 绑了 IP 白名单 | 去掉 CIDR，或换回白名单内 IP |
| `404` 但组织确实存在、token 也新 | 可能是异步还没落地 | 等 100 秒再查 |
| `401` | token 过期/被吊销 | 重建 |

> `404` 是最有欺骗性的一个：它既可能是「scope 不存在」，也可能是「异步未落地」，
> 还可能是「无权访问」。**唯一能区分的是 npm 调试日志里的 PUT 那行。**

### 1.6 发完验证（不要只看元数据）

```bash
cd /tmp && rm -rf npmreal && mkdir npmreal && cd npmreal
npm init -y >/dev/null && npm install @bitqyh/dsh-codex-bridge
./node_modules/.bin/dsh-codex-bridge doctor
./node_modules/.bin/dsh-codex-bridge sessions --from-agent codex --json --all-cwds

# 子路径导出是 DSH 加载插件的关键，必须能解析
node -e "
const {createRequire}=require('module');
const r=createRequire(process.cwd()+'/package.json');
for (const s of ['@bitqyh/dsh-codex-bridge/ui','@bitqyh/dsh-codex-bridge/client'])
  { r.resolve(s); console.log('✅', s); }
"
```

---

## 二、GitHub tag + Release

```bash
git tag -a v0.1.0 -m "v0.1.0 — <一句话要点>"
git push origin v0.1.0
```

> **可能报 `! [remote rejected] … (Internal Server Error)`。** 这是 GitHub 的瞬时
> 故障，等几秒重试即可，不是凭证问题。实测 tag 推送和分支推送都各遇到一次。

建 Release，Notes 里至少要写：**安装命令、运行时依赖表、本版内容**。
依赖表不能省——`python3` 缺了是静默失败，用户装完发现列表空着会以为插件坏了。

```bash
gh release create v0.1.0 --title "..." --notes-file /tmp/notes.md
```

---

## 三、市场 PR（awesome-dsh-plugin）

市场列表在 https://github.com/awesome-dsh-plugin/awesome-dsh-plugin ，
dsh-market 只收录这个 registry 里的条目。

### 3.1 格式

一个 PR **只加一个文件**：`data/plugins/<owner>__<repo>.yml`

```yaml
url: https://github.com/bitqyh/dsh-codex-bridge
name: bitqyh/dsh-codex-bridge
category: session
description:
  en: '一句话说清做什么。'
  zh: '中文一句话。'
```

硬性规则：

- `url` 必须与仓库完全一致；文件名必须是 `<owner>__<repo>.yml`
- `category` 取值见 [contributing.md](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/blob/main/contributing.md)
- `description.en` 必填，**以句号结尾**
- 描述里有 `: `（冒号+空格）必须加引号，否则 YAML 解析失败
- **不要写 `npm:` 字段**，registry 自己从 npm 采集
- 一个 PR 最多 3 个条目
- 仓库的 `package.json` 必须声明 `dsh.bundle`（只有 `dsh.client` 不算可安装）
- 仓库创建满 **1 天**、加 `dsh-plugin` topic

### 3.2 提交流程

```bash
gh repo fork awesome-dsh-plugin/awesome-dsh-plugin --clone=false

cd /tmp && git clone --depth 1 https://github.com/awesome-dsh-plugin/awesome-dsh-plugin.git awcheck
cd awcheck

# ⚠️ clone 出来的 origin 指向上游，推不上去。必须改到自己的 fork：
git remote set-url origin https://github.com/<you>/awesome-dsh-plugin.git

# 先查有没有重名条目
ls data/plugins/ | grep -i "<关键词>"

# 写条目、确认只改一个文件
git checkout -b add-<owner>-<repo>
git add data/plugins/<owner>__<repo>.yml
git commit -m "Add <owner>/<repo> (一句话)"
git push origin add-<owner>-<repo>

gh pr create --repo awesome-dsh-plugin/awesome-dsh-plugin \
  --head <you>:add-<owner>-<repo> --base main \
  --title "Add <owner>/<repo> (…)" --body-file /tmp/pr-body.md
```

本仓库实际执行过的版本（可直接照抄改关键词）：

```bash
gh repo fork awesome-dsh-plugin/awesome-dsh-plugin --clone=false

cd /tmp && git clone --depth 1 \
  https://github.com/awesome-dsh-plugin/awesome-dsh-plugin.git awcheck
cd awcheck
git remote set-url origin https://github.com/bitqyh/awesome-dsh-plugin.git

ls data/plugins/ | grep -i codex          # 查重名

git checkout -b add-bitqyh-dsh-codex-bridge
git add data/plugins/bitqyh__dsh-codex-bridge.yml
git commit -m "Add bitqyh/dsh-codex-bridge (project-grouped Codex chat handoff)"
git push origin add-bitqyh-dsh-codex-bridge

gh pr create --repo awesome-dsh-plugin/awesome-dsh-plugin \
  --head bitqyh:add-bitqyh-dsh-codex-bridge --base main \
  --title "Add bitqyh/dsh-codex-bridge (project-grouped Codex chat handoff)" \
  --body-file /tmp/pr-body.md
```

> **`--body` 不要内联长文本。** 描述里有反引号和引号时，shell 会报
> `unexpected EOF while looking for matching '"'`。用 `--body-file` 写文件传。

### 3.3 PR 描述怎么写（决定会不会被打回）

评审会**读源码逐句核对描述**，最容易被打回的两点：

1. **描述夸大** — 写了数字就要真有那个数字，点了某个 API 名就要真有那个 API
2. **与已有条目重复** — 有自己的差异就必须拿源码比对出来

第 2 点的写法参考（本仓库实际用的）：

> 最接近的条目是 `kanchengw/dsh-assembly.resume`，它也按项目分组。两点差异都可验证：
> **① 组名来源**：它的 `projectNameFromPath()` 取路径最后一段；本插件用
> `projects.name`。实测本机 19 个注册根里 **9 个**项目名不等于路径末段。
> **② 去向**：它把会话导入 DSH 并绑定工作区；本插件只插入草稿，不导入、不新建会话。

有具体数字和文件名的对比，比任何形容词都有说服力。

### 3.4 CI 与评审

- CI 跑 `Submission gate`（条目数、`dsh.bundle`、仓库年龄）+ `awesome-lint` + 站点构建
- 实测耗时 **约 19 分钟**，不是几分钟就绿
- `mergeStateStatus: CLEAN` 且**没有 `held: author` 标签** = 没被打回，在等人工评审
- 维护者要改会给 `held: author`：推到**同一分支**再留**一条**评论说明即可，
  反复推送和追问不会加速
- 合并后站点和 dsh-market 自动重建，通常 1 天内可见

查询状态：

```bash
gh pr view <n> --repo awesome-dsh-plugin/awesome-dsh-plugin \
  --json state,mergeable,mergeStateStatus,labels
gh pr checks <n> --repo awesome-dsh-plugin/awesome-dsh-plugin
```

---

## 四、下一版发版清单

```bash
# 1. 改版本号（并同步 CHANGELOG.md）
npm version patch|minor|major --no-git-tag-version

# 2. 跑测试 + 看打包清单
npm test
npm pack --dry-run

# 3. 扫密钥
grep -rniE "AI_MEMORY_AUTH_TOKEN=[0-9a-f]{8,}|gho_[A-Za-z0-9]{20,}" \
  --include="*.py" --include="*.js" --include="*.md" . | grep -v node_modules

# 4. 提交 + 推送
git add -A && git commit -m "chore: vX.Y.Z" && git push

# 5. 发布（新 token，记得留空 IP ranges）
npm publish --//registry.npmjs.org/:_authToken=npm_xxxxx

# 6. 等 200 再验证
# 7. tag + Release
git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z
gh release create vX.Y.Z --title "..." --notes-file /tmp/notes.md

# 8. 市场条目不用重提（version 从 npm 自动采集），
#    除非改了仓库地址 / 包名 / 分类
```

**已收录后的版本更新不需要重提 PR**——registry 从 npm 读 version。
只有改仓库 URL、包名或分类时才需要 PR 更新条目。

---

## 五、插件装了但界面没反应？先查 patch 的 name 形态

**这是本项目踩过的真实故障，症状极具欺骗性：服务端一切正常，按钮就是不出现。**

`cordis.patch.yml` 的 `name` 必须是 **pathLike 形式**：

```yaml
- insert:
    - id: dsh-codex-bridge-ui
      name: './src/ui-server.js'      # ✅ 可移植：按 patch 所在目录锚定
```

**不能**写成「包名 + 子路径」：

```yaml
      name: '@bitqyh/dsh-codex-bridge/ui'   # ❌ 被静默跳过
```

宿主的 `exactPackageSpecifier` 只接受不含 `/` 的包名，带 `/` 直接返回
`undefined`，`locatePkgJson` 随即 `return`，条目被丢弃且**不报错**。
`parsePatchList` 会把 `./` 按 patch 文件所在目录转成 `file://` URL，
所以装到任何目录都能解析到各自的 `ui-server.js`，不影响可移植性。

排查顺序：

```bash
# 1. 服务端是否正常（正常不代表客户端加载了）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:60762/api/dsh-codex-bridge/sessions
# 401 是正常的（需要 owner 认证）；500/404 才是服务端问题

# 2. 包内 patch 的 name 形态
grep "name:" cordis.patch.yml

# 3. profile 里是否残留重复条目（会导致 readOnlyReason=unaddressable）
grep -c "id: dsh-codex-bridge-ui" ~/.dsh/profiles/desktop/cordis.patch.yml
# 应为 0——入口由包内 bundle patch 提供，profile 不能再手工 insert 同 id

# 4. factory id 是否等于 manifest name（scoped 包用全名）
grep "id:" src/ui-client.js | head -1
```

改完 patch 后：bundle patch 在**进程启动时**读取，所以要么重启 DSH，
要么用「解除卡顿」重建页面。只改文件不重建看不到效果。

详细根因与宿主源码定位见本机记录：
`~/Documents/GPToutput/ai-memory跨agent交接/插件加载恢复记录-2026-11-02.md`

## 六、发完记得

- **删 token**：https://www.npmjs.com/settings/yydsqyh/tokens
- 确认 `npm view <pkg> version` 是新版本
- 若改了包名，同步改：`package.json name`、`cordis.patch.yml` 的 `name:`、
  `src/ui-client.js` 的 `__ModuleLoader__.load({ id })`、README 安装命令。
  **factory id 必须等于 manifest name**（scoped 包用全名），否则宿主 graph 对不上、按钮不渲染。

---

## 环境参考

首次发布时的环境：npm 11.13.0 / node v24.16.0 / python3 3.14.7（macOS）。
测试脚本要求 python 3.8+，只用标准库。

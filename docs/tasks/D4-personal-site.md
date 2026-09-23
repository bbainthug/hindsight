# 任务 D-4：个人站（bbainthug.tech）

> 交付给实现 agent 的任务书。这是一个**独立仓库**（建议 `~/Documents/bbainthug-site`），
> 不在 hindsight 仓库里；hindsight 只借它的域名子域 `brain.`。

## 目标

一个只属于我的网站：记录所见所想、收藏音乐和电影、放一些自己的东西。干净、深色、手机好看。
**公开**，不挂真名，域名就是笔名。（原计划私有；后来想清楚了：这个站的用途是表达，不是存档——存档 Hindsight 已经做了。）

## 已有的前提

- 域名 `bbainthug.tech` 在 Cloudflare（Free），`brain.` 子域已被 Hindsight 占用，根域和其他子域空着
- Cloudflare 账号已有 Zero Trust Free（Access 可用）
- 我在用 Obsidian 写 Markdown

## 核心想法

**网站 = 一个文件夹里的 Markdown，没有后台。** 这个 `content/` 文件夹同时是：
网站内容源、Obsidian 库（直接打开它写）、Hindsight 的 vault（`search_vault` 直接读，不导入）。
agent 写一个 `.md` 就等于"发了一条 + 进了记忆库"。

## 范围

1. Astro 静态站骨架，部署到 **Cloudflare Pages**，绑定根域 `bbainthug.tech`
2. `content/` 五个集合：`notes/`（想）、`music/`（听）、`films/`（看）、`looks/`（穿 / 视觉，图优先）、
   单页 `now.md`（现在）与 `about.md`；标签横跨所有集合
3. 媒体：图片放 `content/_media/`（单文件 < 5 MB）；音频等大文件走 **Cloudflare R2**（免费 10 GB），
   站内 `<audio>` 播放，R2 通过 `media.bbainthug.tech` 提供
4. 访问控制：**不挂 Access**，站点和 `media.` 都公开；只用 Cloudflare 默认的防护
5. 发布：`git push main` → Pages 自动构建；本地 `npm run dev` 预览；
   一个 launchd/cron 任务每小时 `git add content && git commit && git push`（有变化才提交）
6. **agent 一键记**：仓库内提供 Claude Code 技能 `.claude/skills/log/SKILL.md`（Codex 同理放 AGENTS.md
   一段）：用户说"记一下 …"，agent 按集合模板生成 frontmatter + 正文写入 `content/<集合>/YYYY-MM-DD-slug.md`，
   图片一并拷进 `_media/`，然后 `git commit`。技能里写清楚：不得编造字段，不确定就留空。
7. `scripts/upload-media.sh <文件>`：传到 R2 并打印可用 URL
8. 在 Hindsight 侧只需一处配置：`vault.root` 指向 `content/`，`vault_include: ["**/*.md"]`——写进 README，
   不改 Hindsight 代码

## 非目标

- 不做评论、不做搜索、不做登录系统（Access 就是登录）
- 不做 CMS 后台；写作就是改 Markdown
- 不接 Hindsight 的数据；两个站互不依赖
- 不用任何需要付费的服务

## 设计要求（方向 A：黑胶杂志）

- **配色**：背景 `#0e0e0e`，正文 `#e8e4dc`，次要文字 `#8a867e`，细线 `#2a2a2a`；
  亮色模式反过来（`#f4f1ea` 底、`#111` 字）。不要第三种颜色，链接用下划线不用颜色。
- **字体**：标题衬线（自托管，候选 Instrument Serif / Newsreader / 思源宋体做中文回退），
  正文无衬线 16–17px 行高 1.7，日期 / 标签 / 元信息全部等宽 12px。只有这三种字体、两种字重。
- **图片**：1px `#2a2a2a` 边框，圆角 0，可选一层很轻的噪点叠加（CSS，不要图片滤镜库）。
- **不要**：卡片堆叠、阴影、渐变、图标库、动画（除了 hover 下划线）。
- **导航**：顶栏一行：想 / 听 / 看 / 穿 / 现在 / 关于，等宽小字。
- **首页 = 时间流**：所有集合混排倒序，每条一小块（日期 · 类型 · 标题 · 一行摘要或一张小图），
  像一本日记本；分类页只是过滤器。
- **notes**：frontmatter `title, date, tags?`；列表按日期倒序；正文支持图片、引用、代码。
- **music**：frontmatter `title, artist, year?, cover?, audio?（R2 URL）, note?`；列表是封面网格，
  点开显示封面 + 播放器 + 我的一句话。
- **films**：frontmatter `title, director?, year?, poster?, rating?（1–5）, note?`；同上，网格 + 详情。
- **looks**：frontmatter `title?, date, images[], note?, tags?`；列表是不规则图墙，详情大图 + 一句话。
- **now**：单页 Markdown，顶部显示"更新于 YYYY-MM-DD"。
- 所有页面移动端优先，Lighthouse 性能 ≥ 95，构建产物不引任何外部 CDN 或第三方脚本。
- Astro 内容集合用 `zod` schema 校验 frontmatter，缺字段构建直接失败，不要静默。
- 提供 `content/_templates/` 三个模板文件，方便我在 Obsidian 里复制新建。

## 部署与安全

- Pages 项目名 `bbainthug-site`，生产分支 `main`，自定义域 `bbainthug.tech` + `www` 重定向到根。
- 不挂 Access。Pages 的 `*.pages.dev` 预览域名禁用或加 robots noindex，只留自定义域。
- R2 bucket `bbainthug-media`，通过 `media.` 自定义域公开读；不开 bucket 的通用公共 URL（r2.dev）。
- 仓库里不出现任何 token；wrangler 登录用 `wrangler login`（浏览器授权），不落地 API key。
- 想临时转私有：给域名加一个 Access 应用即可，站本身不用改；写进 README。

## 交付

- 仓库 + README（本地预览 / 新建一篇 / 传一个音频 / 切公开 四段）
- `npm run build` 零警告；三个集合各放 2 条示例内容（占位，不用真实信息）
- 部署后的地址、Access 生效截图（curl 得到 302 到 cloudflareaccess）
- 未解决问题

## 已知取舍

- Astro 而不是 Next/Hugo：Markdown 内容集合是它的核心场景，零 JS 默认，主题少但足够；我要的是干净不是功能。
- Pages 而不是放 VM：静态站不需要服务器；VM 只有 1 GB 内存留给 Hindsight。
- 视频不建议整部上传（10 GB 免费额度几部就满），放海报和短评；真要放走 R2 付费（$0.015/GB/月）。
- 公开但不挂真名：想让懂的人看到，不想让认识的人对号入座。

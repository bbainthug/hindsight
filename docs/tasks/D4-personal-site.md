# 任务 D-4：个人站（bbainthug.tech）

> 交付给实现 agent 的任务书。这是一个**独立仓库**（建议 `~/Documents/bbainthug-site`），
> 不在 hindsight 仓库里；hindsight 只借它的域名子域 `brain.`。

## 目标

一个只属于我的网站：记录所见所想、收藏音乐和电影、放一些自己的东西。干净、深色、手机好看。
默认**私有**（Cloudflare Access 登录墙，只有我的邮箱能进），但结构上随时能切公开。

## 已有的前提

- 域名 `bbainthug.tech` 在 Cloudflare（Free），`brain.` 子域已被 Hindsight 占用，根域和其他子域空着
- Cloudflare 账号已有 Zero Trust Free（Access 可用）
- 我在用 Obsidian 写 Markdown

## 范围

1. Astro 静态站骨架，部署到 **Cloudflare Pages**，绑定根域 `bbainthug.tech`
2. 内容目录用 Markdown/MDX，分 4 类集合：`notes/`（所见所想）、`music/`、`films/`、`about`
3. 媒体：图片直接进仓库（单文件 < 5 MB）；音频等大文件走 **Cloudflare R2**（免费 10 GB），站内用
   `<audio>` 播放，R2 通过自定义子域 `media.bbainthug.tech` 提供
4. 访问控制：Pages 域名挂 Cloudflare Access（Allow：<我的邮箱>），`media.` 同样挂
5. 发布流程：`git push main` → Pages 自动构建；本地 `npm run dev` 预览
6. 一个上传脚本 `scripts/upload-media.sh <文件>`：把音频/视频传到 R2 并打印可用 URL

## 非目标

- 不做评论、不做搜索、不做登录系统（Access 就是登录）
- 不做 CMS 后台；写作就是改 Markdown
- 不接 Hindsight 的数据；两个站互不依赖
- 不用任何需要付费的服务

## 设计要求

- **风格**：深色为主（也要有亮色适配），大量留白，一种正文字体 + 一种等宽字体，字号 16–18px，
  行高 1.7。不要卡片堆叠、不要渐变、不要图标库。像一本安静的笔记本。
- **导航**：顶栏只有 4 个词：所想 / 音乐 / 电影 / 关于。首页是最近 10 条所想的列表。
- **notes**：frontmatter `title, date, tags?`；列表按日期倒序；正文支持图片、引用、代码。
- **music**：frontmatter `title, artist, year?, cover?, audio?（R2 URL）, note?`；列表是封面网格，
  点开显示封面 + 播放器 + 我的一句话。
- **films**：frontmatter `title, director?, year?, poster?, rating?（1–5）, note?`；同上，网格 + 详情。
- 所有页面移动端优先，Lighthouse 性能 ≥ 95，构建产物不引任何外部 CDN 或第三方脚本。
- Astro 内容集合用 `zod` schema 校验 frontmatter，缺字段构建直接失败，不要静默。
- 提供 `content/_templates/` 三个模板文件，方便我在 Obsidian 里复制新建。

## 部署与安全

- Pages 项目名 `bbainthug-site`，生产分支 `main`，自定义域 `bbainthug.tech` + `www` 重定向到根。
- Access 应用：`bbainthug.tech`、`www.bbainthug.tech`、`media.bbainthug.tech` 三个 destination，
  同一条 Allow 策略（Emails = <我的邮箱>），会话 1 个月。
  Pages 的 `*.pages.dev` 预览域名也要挂 Access 或禁用，不能留后门。
- R2 bucket `bbainthug-media`，**不开公共读**，只通过 `media.` 自定义域（在 Access 后面）访问。
- 仓库里不出现任何 token；wrangler 登录用 `wrangler login`（浏览器授权），不落地 API key。
- 切公开的方法写进 README：删掉 Access 应用即可，站本身不用改。

## 交付

- 仓库 + README（本地预览 / 新建一篇 / 传一个音频 / 切公开 四段）
- `npm run build` 零警告；三个集合各放 2 条示例内容（占位，不用真实信息）
- 部署后的地址、Access 生效截图（curl 得到 302 到 cloudflareaccess）
- 未解决问题

## 已知取舍

- Astro 而不是 Next/Hugo：Markdown 内容集合是它的核心场景，零 JS 默认，主题少但足够；我要的是干净不是功能。
- Pages 而不是放 VM：静态站不需要服务器；VM 只有 1 GB 内存留给 Hindsight。
- 视频不建议整部上传（10 GB 免费额度几部就满），放海报和短评；真要放走 R2 付费（$0.015/GB/月）。
- 默认私有是因为我说过"不想让别人看"；公开只是删一个 Access 应用。

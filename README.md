# GOSIM 初赛 · 团队做题仓库

> 队长侧维护 · 组员拉取即用：看题 → 练手 → 提交
> 赛期：2026-09-24 00:00 ~ 09-30 23:59（前 20 名晋级决赛 10/1-10/7）

## 📋 赛事一句话

初赛共**两个任务**（Sheet 电子表格 + GitHub 协作平台），每次提交同时评测两个。提交物 = **一个通用智能体**（`main.py` + `requirements.txt` 打包 zip），由平台在 1 核 CPU / 2GB 内存容器里运行，生成目标 web 应用，再跑 Playwright 测试计分。

**评分 = GUI 通过率 × Token 效率 × 完成时间**（三项机器采集）。

## 📂 仓库结构

```
gosim-hackathon/
├── requirements/          # 官方赛题需求文档（组员读题）
│   ├── sheet/             #   Sheet 赛道：在线电子表格
│   │   ├── requirements.md    # 人类可读版（100 测试 / 24 模块）
│   │   └── requirements.yaml  # 机器可读版（唯一权威）
│   └── github/            #   GitHub 赛道：简化协作平台
│       ├── requirements.md    # 人类可读版（100 测试 / 47 模块）
│       └── requirements.yaml  # 机器可读版（唯一权威）
├── practice/              # 官方公开练习环境（本地模拟评测）
│   ├── bookstack/         #   练习1：书栈管理系统
│   └── keep/              #   练习2：Keep 笔记
└── submission/            # 提交工具与契约
    ├── local_submit.py    # 本地模拟提交（Docker 容器评测）
    ├── submit.sh          # 快捷入口（Linux/macOS）
    └── submit.ps1         # 快捷入口（Windows PowerShell）
```

## 🚀 组员快速开始（3 步）

### 第 1 步：拉取仓库

```bash
git clone https://github.com/jackyjinggit/gosim-hackathon.git
cd gosim-hackathon
```

### 第 2 步：读题（选一个赛道主攻）

- **Sheet 赛道**（推荐新手）：`requirements/sheet/requirements.md` —— 做一个类 Google Sheets 的电子表格：工作簿管理、单元格编辑、公式计算、排序筛选、数据验证、透视分析。
- **GitHub 赛道**：`requirements/github/requirements.md` —— 做一个简化 GitHub：账号注册登录、仓库管理、Issue、PR、权限。

> 每个 REQ 下有 ATOMIC 需求 + 具体场景（GIVEN/WHEN/THEN），这是评测测试的直接来源。**读 yaml 为准**（md 由 yaml 生成）。

### 第 3 步：练手（官方公开题）

`practice/` 下是两个官方公开练习（bookstack / keep），可先在本地练：
- 读题：`practice/<练习名>/requirements/requirements.md`
- 看测试：`practice/<练习名>/tests/*.spec.ts`（Playwright 测试即评分标准）

## 🐳 本地模拟提交（可选，需 Docker）

> 官方本地模拟 = 用 Docker 容器 1:1 复现正式评测环境（1 核 / 2GB，Playwright + Chromium）。装 Docker Desktop（Windows）后：

```powershell
cd submission
.\submit.ps1 -Agent "你的agent.zip" `
  -RequirementsDir "..\requirements\sheet" `
  -TestsDir "..\practice\bookstack\tests" `
  -OutputDir ".\runs\run-001"
```

- 你的 agent zip 根目录必须含 `main.py` + `requirements.txt`
- `main.py` 签名：`python3 main.py <需求目录> --output-dir <输出目录>`
- 输出目录必须含 `frontend/` + `backend/`（正式平台只认这个；只发 `deploy.sh` 本地能跑但正式零分）
- 模型环境变量写 `.env`（OPENAI_API_KEY 等），**不要把 key 打进 agent zip**

## 📤 正式提交（队长统一执行）

- 正式提交走网页端队长账号（平台内置额度，**不要用自己的 api_key**——会变成练习通道不计排名）
- 提交后核验 `billing_mode=official_evaluation`（计排名）非 `self_funded`（练习）
- 组员产出 agent zip 后，交队长统一投递

## 🔗 任务板

- 全员任务板（飞书）：[GOSIM 初赛 · 全员任务板](https://ucnrju0zujaw.feishu.cn/docx/Kt2ndmSGEotbHFxjcvIcB0HLnuc)
- 认领支援位：B1 测试编写 / B2 需求拆解 / B3 文档整理（A 组主攻已由队长侧完成，本地 100/100）

## ⚠️ 红线（别踩）

1. **不预实现赛题业务代码**——提交物必须通用 harness + 运行时生成，带答案代码 = 审核风险
2. **不传自己的 api_key**——正式通道用平台内置，自己 key = 练习不计排名
3. **不 0 token**——规则明令不允许，且计分无奖励空间
4. 判定只认 `test.status`，不看 `spec.ok`（有假红）

---

*GOSIM 初赛 · Team 7d159500 · 队长：蒋平 · 更新 2026-09-25*

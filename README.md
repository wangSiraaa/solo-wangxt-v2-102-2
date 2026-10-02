# 频谱工作台（通信工程教学 · 离线简化模型）

用于比较一组载波频率配置的教学工具：录入少量载波与保护间隔后，绘制频段占用与发射谱，
检查 **频带重叠、保护带不足、掩模尾部越界**，在**线性域 (W)** 汇总功率后以 dBm 显示；
极化复用由输入规则决定（禁止 / 允许 / 隔离度未知待评估）；并可用 **OR-Tools CP-SAT**
为载波寻找一组满足间隔的频率位置。场景还可保存**版本化的频谱分配方案**：多个互不
连续的可用频段与不能跨越的排除窗（保护空洞），规划让每条载波完整落在某一有效净空内；
规划结果保存为不可变快照，编辑分配方案后经版本与 post-check 标为过期而不会被偷偷移动。

> ⚠️ 纯离线简化模型：**不连接无线电设备，不生成任何发射指令**。掩模与发射谱均为教学
> 用平顶 + 折线衰减的简化数值，不对应任何标准或真实设备指标。

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | React 18 + Vite + Plotly.js |
| 后端 | FastAPI + Pydantic + SQLAlchemy 2 |
| 科学计算 | SciPy（掩模分段线性插值、PSD 积分）、NumPy |
| 约束求解 | OR-Tools CP-SAT（1 kHz 频率网格） |
| 数据库 | PostgreSQL（中心频率、带宽、功率、示例掩模、场景） |

## 目录

```
backend/   FastAPI 应用
  app/services/units.py       dBm<->W、线性域功率汇总
  app/services/masks.py       掩模折线、发射谱曲线（SciPy interp1d）
  app/services/analysis.py    三类冲突检查（定位到载波对/方向）
  app/services/allocation.py  多段可用频段 + 排除窗、有效净空小片、版本指纹
  app/services/planner.py     OR-Tools CP-SAT 多段频率规划
  app/plan_records.py         规划快照的内容指纹与 post-check 过期判定
  app/db.py / seed.py         SQLAlchemy 模型与教学演示场景（两段 + 空洞）
frontend/  React + Plotly.js（分配方案编辑、边界/排除区/归属绘图、规划历史）
scripts/   start-dev.sh       一键启动（本地 PostgreSQL + 后端 + 前端）
```

## 快速开始

需要本机 PostgreSQL（或修改 `DATABASE_URL`）。提供一个 conda 方式安装的便捷脚本：

```bash
# 1) Python 依赖
pip install -r backend/requirements.txt

# 2) 前端依赖
cd frontend && npm install && cd ..

# 3) 初始化数据库并写入示例掩模 + 教学演示场景
python -m app.seed          # 工作目录为 backend/，或设置 PYTHONPATH

# 4) 启动
( cd backend && uvicorn app.main:app --reload --port 8000 )
( cd frontend && npm run dev )        # http://localhost:5173 ，/api 代理到 8000
```

环境变量：`DATABASE_URL`（默认 `postgresql+psycopg2://postgres@127.0.0.1:5432/spectrum`）、
`CORS_ORIGINS`。

## 计算模型要点

### 1. 冲突检查（`POST /api/analyze`，结果含 bands / spectrum / findings / power_summary）

- **频带重叠 overlap**：两频带边缘净距 `gap = max(low) − min(high) ≤ 0`，报重叠量。
- **保护带不足 guard_shortfall**：`0 < gap < guard_required_mhz`，报净距与缺口。
- **掩模尾部越界 mask_tail（有方向）**：发射机掩模谱落入受害载波频带的功率
  `P = ∫_victim 10^(PSD_dbm/10)·1mW df`（PSD 在线性 W/Hz 域积分），超过限值即报；
  因功率/掩模不同，A→B 与 B→A 分别计算、分别定位。
- **极化复用规则**（针对异极化对）：
  - `forbidden` 禁止：重叠即冲突；
  - `allowed` 允许（已知隔离度足够）：可同频，不报几何/泄漏冲突；
  - `unknown` 待评估：重叠只提示“隔离度未知，复用待评估”，不下违规定性；
  - 同极化对无极化隔离，始终按禁止处理。

### 2. 功率汇总

dB 是对数尺度，**不能直接相加**。先换算
`P_W = Σ 1mW · 10^(p_i/10)`，再 `P_dBm = 10 log10(P_W/1mW)`。
响应里同时给出错误的“dBm 直接求和”数值供课堂对比（例如多个载波 275 dBm vs 正确 36.14 dBm）。

### 3. OR-Tools 规划（`POST /api/plan`）与频谱分配方案

**频谱分配方案**（版本化，可随场景保存）由两部分组成：

- `segments`：一个或多个互不重叠、按频率排序的**可用频段**，段与段之间是不可跨越的
  频率空洞；
- `exclusions`：若干**排除窗**（保护空洞 / 其他业务占用），可落在段内，把该段切成
  更小的有效净空小片（piece），也可覆盖段间区域。

载波的占用带宽必须**完整落在某个有效净空小片内**——不能跨越段间空洞，也不能覆盖
任何排除窗。旧场景只有单一可用范围 `[band_low, band_high]`，读取/调用时自动迁移为
等价的单段方案（响应里以 `allocation_migrated: true` 标注）。

CP-SAT 在 1 kHz 网格上为每个载波选择**中心频率 + 所属净空小片**，约束：

- 每载波恰好选中一个放得下其占用带宽的小片（`AddExactlyOne`），小片边界即中心频率的
  上下界；放不下任何小片（宽度大于任一可用段、或只能跨越排除窗）时明确返回
  `INFEASIBLE` / `MODEL_INVALID` 与 `infeasible_reasons`（哪条载波、最宽净空是多少、
  录入位置跨越了哪些排除窗）；
- 需要隔离的载波对（同极化 / 禁止 / 待评估）通过布尔析取排序，满足
  `中心距 ≥ 半宽_i + 半宽_j + 要求边缘净距`；分处不同小片的载波天然被空洞隔开；
  同/异极化与 forbidden/unknown/allowed 规则不因分段而改变——`allowed` 才可同址；
- `guard_only`：统一用保护间隔；`mask_aware`：对每对载波按**双向**掩模泄漏都不越限
  反算所需净距（含 0.5 dB 规划裕量）；
- 目标：最小化相对录入位置的总偏移。每条返回的载波都带 `segment_index` /
  `piece_mhz` 与 `assignment_reason`（说明为何落在该段）。

响应含 `allocation`（段边界、排除区、有效小片，供绘图）、`bands`（每条载波的归属
状态 inside / spans_gap / crosses_exclusion / outside_segments）与 `post_check`。

#### 版本、过期与快照

- 分配方案带 `version`：仅当段/排除窗的几何内容变化时，保存场景由服务端权威 +1；
  只改方案名、或内容不变，版本保留。
- `POST /api/scenarios/{id}/plans` 把一次规划存为**不可变快照**（绑定当时的分配版本、
  载波/规则指纹与 post-check 基线计数）。
- 之后编辑频段（例如把某段边界缩到旧方案占用之外）**不会移动旧规划**；读取快照
  （默认 `refresh=true`，也可显式刷新）时在当前场景上重跑 post-check 并核对每条载波
  是否仍完整位于某有效小片内，任一不满足即置 `stale=true`，给出越界载波与原因；
  原始频率、生成时版本与历史结论始终保留可追溯。
- 导出（`GET /api/scenarios/{id}/export`）包含分配方案与全部规划历史；导入为
  **原子操作**——段重叠、排除窗悬空、区间倒置或快照缺字段时整份拒绝（400），
  不会部分保存。

分析图同时显示：可用段底带与边界（S1/S2…）、红色排除区、录入实际占用（按冲突/归属
着色）与规划后按所属段着色的归属描边框。

## 教学演示场景（种子数据，12 个载波，带宽均为 4 MHz、功率不同）

| 载波对 | 现象 |
|---|---|
| C1(30 dBm,loose) / C2(20 dBm,loose)，净距 0.5 MHz | 保护带不足 + 双向尾部越界，泄漏 15.4 vs 5.3 dBm（功率不同导致方向不对称） |
| C4(30,strict) / C5(25,loose)，净距 0.5 MHz | 保护带不足；仅弱载波 loose 尾部越界（强→弱 −8.6 dBm 达标边界，弱→强 10.3 dBm 越界） |
| C7(30,loose) / C8(20,strict)，净距 2 MHz | 保护带虽够，强载波拖尾仍越界 2.0 dBm；反向 −47.5 dBm 达标 |
| C9 / C10，重叠 0.5 MHz | 同极化频带重叠 |
| C1(H) / C6(V) 同频 | 规则 unknown → “复用待评估” |
| C11(RHCP) / C12(V) 同频 | 规则 allowed → 无冲突 |

## 测试

```bash
# 纯函数 + 规划 + API（API 测试用内存 SQLite，无需外部服务）
PYTHONPATH=backend pytest backend/tests -q
```

48 个测试覆盖：dBm/W 换算、三类冲突对定位、泄漏方向性、三种极化规则、
两种规划模式的可行性与规划后零越界、场景 CRUD、多段/排除窗几何校验、
多段规划分段落位与不可跨洞、超宽/跨窗载波明确不可行、极化规则不被分段绕过、
旧格式场景迁移、分配版本递增、规划快照在改频段/改规则后经 post-check 标过期
（旧位置不移动、结论可追溯）、导出导入往返与脏数据整体拒绝。

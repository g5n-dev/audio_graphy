# Design QA — 标签洞察关系图谱与图谱稳定性

验收日期：2026-07-24

## 对照基线

- 设计真值：`.design-audit/tag-graph-redesign/01-ai-stack-reference.png`
- 实现截图：`.design-audit/tag-graph-redesign/05-after-cleanup.png`
- 全屏同图对照：`.design-audit/tag-graph-redesign/06-reference-vs-implementation.png`
- 图谱区域同图对照：`.design-audit/tag-graph-redesign/07-focused-graph-comparison.png`
- 状态：参考站 `community` 模式；实现为 `#/tag-insights` 默认“关系图谱”Tab，演示数据，`stage.requirement` 冲突节点已选中。
- CSS 视口：1280 × 720；浏览器 `devicePixelRatio=2`。
- 像素归一化：浏览器截图均输出 1280 × 720，即 1 输出像素对应 1 CSS 像素；同图全屏对照为 2560 × 720。图谱聚焦对照分别裁取参考站 942 × 657、实现 748 × 504，并按近似相同纵横比归一至 940 × 630 后并排。

## Findings

当前没有待修复的 P0/P1/P2 视觉问题。

- 字体与排版：实现沿用 AudioGraphy 的 Inter、苹方和微软雅黑字体栈，不复制参考站的终端字体；中文社区名为主标题、技术 `label_key` 和节点数为次级信息。节点标签在 100% 下可辨认，完整语义仍由 hover、焦点和右侧详情承载。
- 间距与构图：中心目标、八个社区及社区成员形成三级径向层级；社区分布覆盖中心上下左右，不再退化为三列网格。1280 × 720 首屏显示完整中心与全部社区，图例和缩放控件没有遮挡核心节点。
- 色彩与视觉 token：保留参考站的暖色中心、青色关系线和多重社区轮廓，但按用户要求转换成 AudioGraphy 浅色体系。社区颜色按 `label_key` 稳定映射；冲突红、证据灰和一致绿只承担语义状态，不替代社区身份色。
- 图像与资产：该区域是实时数据驱动 SVG 图谱，没有需要复刻的照片、Logo 或插画资产；图标继续使用项目现有 Arco 图标库。未以占位图或装饰图片替代交互节点。
- 文案与内容：社区显示“业务中文名 + 技术键 + 节点数”；中心、关系图例、冲突、版本值和证据时间码均来自现有数据模型，没有伪造参考站内容。
- 交互与可访问性：节点保留鼠标点击、Enter/Space、`aria-pressed`、hover 情报卡、右侧溯源、冲突筛选、重置和 80%–150% 缩放。关系图谱、对比矩阵、图表分析任一时刻只挂载一个 `tabpanel`。

## Comparison history

### Iteration 1 — blocked

- [P1] 多社区使用规则网格，中心位于顶部，缺少参考站“中心—社区—成员”拓扑。
- [P1] 节点使用大矩形卡片，社区内部层级不清，画面更像流程图而不是社群图。
- [P2] 画布最小高度 760px，1280 × 720 首屏只能看到图谱上半部。
- [P2] 每个节点常驻两行文本，缩放后形成明显标签拥挤。
- 证据：`.design-audit/tag-graph-redesign/03-before-comparison.png`。

### Iteration 2 — fixed

- 将全部社区改为围绕中心目标的椭圆轨道布局，社区内部改为确定性多环布局。
- 将焦点、标签值、证据改为不同尺寸的圆形图元；只让标签值常驻短标签，完整信息进入 hover 和详情。
- 增加暖色中心轨道、社区多重轮廓、跨社区虚线和箭头，并使用浅色品牌 token。
- 将画布和检查器高度改为当前视口可见区，SVG 按宽度保持比例缩放。
- 增加中文社区名和稳定颜色映射。
- 后验同图证据：`.design-audit/tag-graph-redesign/06-reference-vs-implementation.png` 与 `.design-audit/tag-graph-redesign/07-focused-graph-comparison.png`。

## 运行与交互验收

- `#/graph` 菜单进入、直接访问和重新加载均显示标题、筛选器与图谱画布。
- 新建浏览器标签完成 `标签洞察 → 全域知识图谱 → 直接刷新`，控制台无 G6 生命周期 error/warning。
- `关系图谱 → 对比矩阵 → 图表分析` 切换期间 Hash 始终为 `#/tag-insights`、`scrollY=0`，DOM 中始终只有一个活动 `tabpanel`。
- 键盘在“对比矩阵”按 ArrowRight 后焦点、`aria-selected` 和活动面板同步切换到“图表分析”。
- 自动化验证：ESLint 通过；Vitest 22 个文件、140 项测试通过；TypeScript 与生产构建通过；Sites 构建通过。

## Follow-up polish

- [P3] 真实生产数据若出现超过 10 个社区，可进一步加入双环社区碰撞松弛；当前 48 节点预算和确定性轨道已保证不产生悬空边。
- [P3] 复杂 SVG 的完整朗读顺序仍建议使用真实屏幕阅读器复核。

final result: passed

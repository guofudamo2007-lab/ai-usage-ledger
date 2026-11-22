# 前端开源组件

以下资源随刻度离线分发。各项目的版权和许可原文随附于 `licenses` 目录。

| 项目 | 版本 | 实际使用 | 许可 |
| --- | --- | --- | --- |
| [uPlot](https://github.com/leeoniya/uPlot) | 1.6.32 | 用量趋势的 Canvas 2D 绘图、比例尺、坐标和游标 | MIT |
| [Tom Select](https://github.com/orchidjs/tom-select) | 2.7.0 | 统计时区和明细条件的搜索、选项列表和键盘交互 | Apache-2.0 |
| [sifter.js](https://github.com/orchidjs/sifter.js) 和 [unicode-variants](https://github.com/orchidjs/unicode-variants) | 1.1.0 / 1.2.2 | Tom Select 基础构建内含的搜索依赖 | Apache-2.0 |
| [Radix Colors](https://github.com/radix-ui/colors) | 3.0.0 | Slate 和 Teal 深色阶 | MIT |
| [Open Props](https://github.com/argyleink/open-props) | 1.7.23 | 片段选择器和能响应减少动态效果偏好的短弹簧过渡 | MIT |
| [Phosphor Icons](https://github.com/phosphor-icons/core) | 2.1.1 | 导航、统计和操作使用的双色矢量图标 | MIT |

构建时合并所用的样式表，移除构建文件的 source map 注释，并将所用的 Phosphor 图标整理为 SVG 符号集。源码中的 `tools/vendor_ui.py` 记录版本、SHA-512 完整性摘要和文件清单。

布局与光学交互参考了 [Obsidian Prism](https://github.com/RMvdesi/obsidian-prism) 的用量面板、光学读数与控件反馈，以及 [Tremor BarList](https://github.com/tremorlabs/tremor/blob/main/src/components/BarList/BarList.tsx) 的排行读法。缓存刻片、入口分布与账本版式在刻度中独立实现。

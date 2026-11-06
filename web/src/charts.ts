// One place that registers the ECharts modules the app uses (tree-shaken, bundled locally).
import * as echarts from "echarts/core";
import { LineChart, ScatterChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  LineChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  DataZoomComponent,
  AriaComponent,
  TitleComponent,
  ToolboxComponent,
  CanvasRenderer,
]);

export { echarts };

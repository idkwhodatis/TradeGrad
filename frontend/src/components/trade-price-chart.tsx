import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  type Time,
  type SeriesMarker,
} from "lightweight-charts";
import type { PriceReplay } from "@/client/types.gen";

export default function TradePriceChart({
  data,
  selectedId,
  onSelect,
}: {
  data: PriceReplay;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const callback = useRef(onSelect);
  callback.current = onSelect;
  useEffect(() => {
    if (!container.current || !data.bars.length) return;
    const chart = createChart(container.current, {
      autoSize: true,
      height: 480,
      layout: { textColor: "#45583c", attributionLogo: true },
      grid: {
        vertLines: { color: "#f0f2ec" },
        horzLines: { color: "#f0f2ec" },
      },
      rightPriceScale: { scaleMargins: { top: 0.12, bottom: 0.25 } },
    });
    const price = chart.addSeries(CandlestickSeries, {
      upColor: "#367653",
      downColor: "#b35748",
      borderVisible: false,
      wickUpColor: "#367653",
      wickDownColor: "#b35748",
    });
    price.setData(
      data.bars.map((bar) => ({
        time: bar.session_date,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      })),
    );
    const volume = chart.addSeries(HistogramSeries, {
      color: "#b5c9a9",
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    volume
      .priceScale()
      .applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    volume.setData(
      data.bars.map((bar) => ({ time: bar.session_date, value: bar.volume })),
    );
    const markers: SeriesMarker<Time>[] = data.markers
      .filter((item) => item.on_price_bar)
      .map((item) => ({
        id: item.id,
        time: item.session_date,
        position: item.side === "sell" ? "aboveBar" : "belowBar",
        shape:
          item.side === "buy"
            ? "arrowUp"
            : item.side === "sell"
              ? "arrowDown"
              : "circle",
        color:
          item.id === selectedId
            ? "#4d42b5"
            : item.side === "buy"
              ? "#367653"
              : "#b35748",
        text:
          item.id === selectedId
            ? `Selected ${item.side} ${item.quantity} @ ${item.price}`
            : item.side,
        size: item.id === selectedId ? 2 : 1,
      }));
    markers.sort((a, b) => String(a.time).localeCompare(String(b.time)));
    createSeriesMarkers(price, markers);
    const panes = new Map<number, number>([[0, 0]]);
    const colors = ["#7656a5", "#147d92", "#af6d24", "#506db1"];
    data.indicators
      .filter((item) => item.status === "available")
      .forEach((item, index) => {
        // Keep declared auxiliary panes separate. Never reinterpret an arbitrary
        // indicator as a price-derived built-in or relabel its declared units.
        if (!panes.has(item.pane)) panes.set(item.pane, panes.size);
        const pane = panes.get(item.pane)!;
        const options = {
          title: `${item.name} (${item.unit})`,
          color: colors[index % colors.length],
          priceScaleId:
            item.pane === 0 ? "right" : `indicator-${item.instance_id}`,
        };
        const series =
          item.render_type === "histogram"
            ? chart.addSeries(HistogramSeries, options, pane)
            : chart.addSeries(LineSeries, options, pane);
        const points = new Map(
          item.points.map((point) => [point.session_date, point.value]),
        );
        series.setData(
          data.bars.map((bar) => {
            const value = points.get(bar.session_date);
            return value == null
              ? { time: bar.session_date }
              : { time: bar.session_date, value };
          }),
        );
      });
    chart.subscribeClick((event) => {
      if (
        typeof event.hoveredObjectId === "string" &&
        data.markers.some((item) => item.id === event.hoveredObjectId)
      )
        callback.current(event.hoveredObjectId);
    });
    chart.timeScale().fitContent();
    const selected = data.markers.find((item) => item.id === selectedId);
    if (selected?.on_price_bar && data.bars.length > 30) {
      const index = data.bars.findIndex(
        (bar) => bar.session_date === selected.session_date,
      );
      chart.timeScale().setVisibleLogicalRange({
        from: Math.max(0, index - 10),
        to: Math.min(data.bars.length - 1, index + 10),
      });
    }
    return () => chart.remove();
  }, [data, selectedId]);
  return (
    <figure className="m-0">
      <div
        ref={container}
        role="img"
        aria-label="Pinned OHLCV, actual execution markers and declared recorded indicators"
      />
      <figcaption className="text-xs muted mt-2">
        Only this bounded price page is plotted. Select a fill below or click
        its marker. Indicator gaps are not interpolated. Charts by{" "}
        <a
          className="text-link"
          href="https://www.tradingview.com/"
          target="_blank"
          rel="noreferrer"
        >
          TradingView Lightweight Charts™
        </a>
        . Copyright (с) 2025 TradingView, Inc.
      </figcaption>
    </figure>
  );
}

import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  createChart,
  HistogramSeries,
  type CandlestickData,
  type HistogramData,
  type Time,
} from "lightweight-charts";

export default function PriceChart({
  candles,
  volumes,
}: {
  candles: CandlestickData<Time>[];
  volumes: HistogramData<Time>[];
}) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!container.current) return;
    const chart = createChart(container.current, {
      autoSize: true,
      height: 320,
      layout: { textColor: "#45583c", attributionLogo: true },
      grid: {
        vertLines: { color: "#f0f2ec" },
        horzLines: { color: "#f0f2ec" },
      },
      rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.25 } },
    });
    chart
      .addSeries(CandlestickSeries, {
        upColor: "#367653",
        downColor: "#b35748",
        borderVisible: false,
        wickUpColor: "#367653",
        wickDownColor: "#b35748",
      })
      .setData(candles);
    const volume = chart.addSeries(HistogramSeries, {
      color: "#b5c9a9",
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    volume.setData(volumes);
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [candles, volumes]);
  return (
    <figure className="m-0">
      <div
        ref={container}
        className="price-chart"
        role="img"
        aria-label="Daily OHLC candlesticks and volume for the selected bounded page"
      />
      <figcaption className="mt-2 text-xs muted">
        Only the selected page is plotted. The table below retains original
        numeric precision. Charts by{" "}
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

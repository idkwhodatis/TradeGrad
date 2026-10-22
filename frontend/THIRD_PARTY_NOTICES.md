# Third-party notices

This frontend adapts the React/Vite application pattern and shadcn UI components from
[FastAPI's official full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template),
commit `b1125d8839482cb7bbae603ddc6d0081bf87532e`, under the MIT license reproduced in
`FASTAPI-TEMPLATE-LICENSE`. Files copied/adapted: `src/components/ui/{button,input,card,badge}.tsx`
and `src/lib/utils.ts`. Authentication, routing, API generation and query-cache structure
follow the template, but the TradeGrad domain pages and behavior are new. Unused template
admin, email and password-reset pages are not shipped.

UI component conventions originate from shadcn/ui (MIT); npm dependencies retain their
respective licenses. The generated `src/client/` files are produced by @hey-api/openapi-ts
from the repository's backend OpenAPI schema, not handwritten API contracts.

## M2 data browsing components

- TanStack Table 8.21.3, MIT: https://github.com/TanStack/table
- TradingView Lightweight Charts 5.1.0, Apache-2.0 with attribution notice:
  https://github.com/tradingview/lightweight-charts
  The chart displays the TradingView attribution logo, copyright notice and a
  visible product link. `LIGHTWEIGHT-CHARTS-NOTICE` reproduces the upstream
  [5.1.0 notice](https://raw.githubusercontent.com/tradingview/lightweight-charts/v5.1.0/NOTICE).
  It is lazy-loaded only for the snapshot preview, not the login/run routes.

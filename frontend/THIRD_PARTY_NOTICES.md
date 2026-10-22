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

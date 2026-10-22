# Third-party provenance

The application skeleton adapts FastAPI's official full-stack template at
`b1125d8839482cb7bbae603ddc6d0081bf87532e`:
https://github.com/fastapi/full-stack-fastapi-template/tree/b1125d8839482cb7bbae603ddc6d0081bf87532e

Its MIT notice is preserved in `FASTAPI_TEMPLATE_LICENSE.txt`. Template-derived
security and frontend component files retain their applicable upstream terms.

The existing TradeGrad code remains based on
`baf73ee56353de8996731139f6721b62f4b26da7`. No root license was present at that
revision. This change does not assign a new license to that code. Public product
redistribution requires resolving its licensing with the rights holder.

Quant engine and package licenses remain those of their locked distributions.
Live provider data rights are separate from SDK licensing. No historical provider
data has been redistributed in the synthetic M0/M1 fixtures.

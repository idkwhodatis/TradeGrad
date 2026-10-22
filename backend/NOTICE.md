# FastAPI template attribution

Authentication (OAuth2 bearer flow, Argon2 password hashing and JWT access tokens),
user model structure, dependency pattern and application layout are adapted from
[fastapi/full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template),
commit `b1125d8839482cb7bbae603ddc6d0081bf87532e`.
Copyright (c) 2019 Sebastián Ramírez. MIT license reproduced in
`LICENSE.fastapi-template`. Domain orchestration is new TradeGrad integration code.

This is an intentionally minimal M1 slice, not a claim that all template account
management, password reset, email or administration features are implemented.

The frozen initial migration includes SQL from Procrastinate 3.10.0, an MIT-licensed
project by the Procrastinate contributors (copyright PeopleDoc and the Procrastinate contributors).
Its license is reproduced in `LICENSE.procrastinate`.

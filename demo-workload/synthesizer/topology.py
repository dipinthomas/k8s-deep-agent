"""Service topology for the retail-prod demo workload.

Each request originates at frontendservice and follows the call graph:

    frontendservice
       └─ checkoutservice (POST /checkout)
            ├─ cartservice (GET /cart/{userId})
            ├─ productcatalogservice (POST /products/lookup)
            └─ paymentservice (POST /charge)              ← the hot path
                 └─ productcatalogservice (POST /reserve)

The hot path through paymentservice is what spikes during a `spike` scenario.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Call:
    service: str
    operation: str
    children: tuple = field(default_factory=tuple)


PAYMENT_RESERVE = Call(
    service="productcatalogservice",
    operation="POST /reserve",
)

PAYMENT = Call(
    service="paymentservice",
    operation="POST /charge",
    children=(PAYMENT_RESERVE,),
)

CHECKOUT = Call(
    service="checkoutservice",
    operation="POST /checkout",
    children=(
        Call(service="cartservice", operation="GET /cart/{userId}"),
        Call(service="productcatalogservice", operation="POST /products/lookup"),
        PAYMENT,
    ),
)

ROOT = Call(
    service="frontendservice",
    operation="POST /api/checkout",
    children=(CHECKOUT,),
)

ALL_SERVICES = (
    "frontendservice",
    "checkoutservice",
    "cartservice",
    "productcatalogservice",
    "paymentservice",
)

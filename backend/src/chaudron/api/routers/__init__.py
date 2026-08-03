"""HTTP routers. One module per resource of the v1 contract."""

from chaudron.api.routers.health import router as health_router
from chaudron.api.routers.inventory import router as inventory_router
from chaudron.api.routers.locations import router as locations_router
from chaudron.api.routers.products import router as products_router
from chaudron.api.routers.providers import router as providers_router
from chaudron.api.routers.recipes import router as recipes_router

__all__ = [
    "health_router",
    "inventory_router",
    "locations_router",
    "products_router",
    "providers_router",
    "recipes_router",
]

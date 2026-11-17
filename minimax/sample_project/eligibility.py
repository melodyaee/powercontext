def can_refund(order: dict) -> bool:
    """Initial implementation: only checks paid status (missing expiry boundary)."""
    return order.get("status") == "paid"

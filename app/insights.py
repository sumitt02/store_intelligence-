"""
Insight Engine — auto-generates actionable store recommendations from analytics data.

Produces ranked insights across: conversion, zone performance, queue management,
product placement, and staffing. Each insight has a severity, business impact
estimate, and a concrete suggested action.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.metrics import compute_metrics
from app.heatmap import compute_heatmap
from app.funnel import compute_funnel
from app.revenue import compute_revenue
from app.journey import compute_journey


INSIGHT_TEMPLATES = {
    "high_dwell_low_traffic": {
        "title": "High dwell, low footfall — hidden gem zone",
        "detail": "{zone} has {dwell}s avg dwell but only {visits} visits. "
                  "Customers who find it, love it. Move it closer to the entrance or add directional signage.",
        "impact": "Estimated +{uplift}% zone visits if repositioned near high-traffic area.",
        "priority": "HIGH",
        "category": "product_placement",
    },
    "high_traffic_low_dwell": {
        "title": "High footfall, low engagement — shelf needs work",
        "detail": "{zone} attracts {visits} visitors but avg dwell is only {dwell}s. "
                  "Customers glance and move on. Consider improving shelf display, pricing visibility, or testers.",
        "impact": "Even +5s dwell in high-traffic zones significantly boosts impulse purchase rate.",
        "priority": "MEDIUM",
        "category": "merchandising",
    },
    "queue_abandonment_high": {
        "title": "Critical: Billing queue driving revenue loss",
        "detail": "{abandon_pct:.0f}% of customers who reached billing abandoned the queue. "
                  "With avg basket ₹{basket:.0f}, each abandonment costs ~₹{loss:.0f} in lost revenue.",
        "impact": "Reducing abandonment by 20% could recover ₹{recovery:.0f}/day.",
        "priority": "CRITICAL",
        "category": "operations",
    },
    "low_conversion_high_footfall": {
        "title": "Strong footfall but poor conversion",
        "detail": "{visitors} customers entered but only {converted} completed a purchase ({rate:.1f}% conversion). "
                  "Industry benchmark for beauty retail is 35-45%.",
        "impact": "Closing the conversion gap to 35% means {gap} additional transactions/day.",
        "priority": "HIGH",
        "category": "conversion",
    },
    "affinity_merchandising": {
        "title": "Cross-sell opportunity: co-visited zones not co-located",
        "detail": "{zone_a} and {zone_b} are visited together in {pct:.0f}% of sessions. "
                  "Place complementary products or cross-promotion displays between them.",
        "impact": "Cross-category promotions in beauty retail average 12-18% basket uplift.",
        "priority": "MEDIUM",
        "category": "merchandising",
    },
    "dead_zone": {
        "title": "Under-performing zone — needs intervention",
        "detail": "{zone} received only {visits} visits all day vs store avg of {avg_visits:.0f}. "
                  "Consider relocating stock, adding a promotional display, or reassigning the space.",
        "impact": "Eliminating dead zones increases overall floor productivity.",
        "priority": "LOW",
        "category": "product_placement",
    },
    "peak_hour_staffing": {
        "title": "Staff peak-hour mismatch risk",
        "detail": "Peak transaction hour is {hour}:00 with {txns} transactions. "
                  "Ensure adequate billing staff and floor staff are scheduled for {hour}:00–{hour_end}:00.",
        "impact": "Queue abandonment spikes 3× when billing wait exceeds 3 minutes.",
        "priority": "MEDIUM",
        "category": "staffing",
    },
    "first_zone_dominance": {
        "title": "Entry zone shapes entire visit",
        "detail": "{zone} is the first zone {pct:.0f}% of customers browse. "
                  "This zone sets purchase intent. Feature high-margin or new-launch products here.",
        "impact": "First-zone product placement can increase overall basket value by 8-15%.",
        "priority": "HIGH",
        "category": "product_placement",
    },
}


def generate_insights(store_id: str, db: Session, date: Optional[str] = None) -> dict:
    now = datetime.now(timezone.utc)
    date_str = date or now.strftime("%Y-%m-%d")

    metrics = compute_metrics(store_id, db, date=date_str)
    heatmap = compute_heatmap(store_id, db, date=date_str)
    funnel = compute_funnel(store_id, db, date=date_str)
    revenue = compute_revenue(store_id, db, date=date_str)
    journey = compute_journey(store_id, db, date=date_str)

    insights = []

    # --- Zone-level insights from heatmap ---
    if heatmap.zones:
        scores = [z.score for z in heatmap.zones]
        visits = [z.visit_frequency for z in heatmap.zones]
        dwells = [z.avg_dwell_ms for z in heatmap.zones]

        avg_visits = sum(visits) / len(visits)
        avg_dwell = sum(dwells) / len(dwells)
        max_visits = max(visits)
        max_dwell = max(dwells)

        for z in heatmap.zones:
            dwell_s = z.avg_dwell_ms / 1000

            # High dwell, low traffic (hidden gem)
            if dwell_s > avg_dwell / 1000 * 1.5 and z.visit_frequency < avg_visits * 0.6 and z.visit_frequency >= 3:
                uplift = round((avg_visits - z.visit_frequency) / avg_visits * 40)
                t = INSIGHT_TEMPLATES["high_dwell_low_traffic"]
                insights.append({
                    "insight_id": f"hdlt_{z.zone_id}",
                    "priority": t["priority"],
                    "category": t["category"],
                    "title": t["title"],
                    "detail": t["detail"].format(zone=z.zone_id, dwell=round(dwell_s, 1), visits=z.visit_frequency),
                    "impact": t["impact"].format(uplift=uplift),
                    "zone": z.zone_id,
                })

            # High traffic, low dwell (shelf needs work)
            elif z.visit_frequency > avg_visits * 1.3 and dwell_s < avg_dwell / 1000 * 0.5:
                t = INSIGHT_TEMPLATES["high_traffic_low_dwell"]
                insights.append({
                    "insight_id": f"htld_{z.zone_id}",
                    "priority": t["priority"],
                    "category": t["category"],
                    "title": t["title"],
                    "detail": t["detail"].format(zone=z.zone_id, visits=z.visit_frequency, dwell=round(dwell_s, 1)),
                    "impact": t["impact"],
                    "zone": z.zone_id,
                })

            # Dead zone
            elif z.visit_frequency < avg_visits * 0.35 and avg_visits > 5:
                t = INSIGHT_TEMPLATES["dead_zone"]
                insights.append({
                    "insight_id": f"dead_{z.zone_id}",
                    "priority": t["priority"],
                    "category": t["category"],
                    "title": t["title"],
                    "detail": t["detail"].format(zone=z.zone_id, visits=z.visit_frequency, avg_visits=round(avg_visits, 1)),
                    "impact": t["impact"],
                    "zone": z.zone_id,
                })

    # --- Queue abandonment ---
    if metrics.abandonment_rate > 0.30:
        abandon_pct = metrics.abandonment_rate * 100
        basket = revenue.get("avg_basket_value_inr") or 1500
        joins = revenue.get("transaction_count") or 10
        abandons = round(joins * metrics.abandonment_rate / (1 - metrics.abandonment_rate))
        loss_per_day = abandons * basket
        recovery = loss_per_day * 0.20
        t = INSIGHT_TEMPLATES["queue_abandonment_high"]
        insights.append({
            "insight_id": "queue_abandon",
            "priority": t["priority"],
            "category": t["category"],
            "title": t["title"],
            "detail": t["detail"].format(abandon_pct=abandon_pct, basket=basket, loss=basket),
            "impact": t["impact"].format(recovery=round(recovery, 0)),
            "zone": "CASH_COUNTER",
        })

    # --- Low conversion ---
    funnel_entry = next((s.count for s in funnel.stages if s.stage == "ENTRY"), 0)
    funnel_purchase = next((s.count for s in funnel.stages if s.stage == "PURCHASE"), 0)
    if funnel_entry >= 10:
        actual_rate = funnel_purchase / funnel_entry
        if actual_rate < 0.30:
            target_txns = round(funnel_entry * 0.35)
            gap = max(0, target_txns - funnel_purchase)
            t = INSIGHT_TEMPLATES["low_conversion_high_footfall"]
            insights.append({
                "insight_id": "low_conversion",
                "priority": t["priority"],
                "category": t["category"],
                "title": t["title"],
                "detail": t["detail"].format(
                    visitors=funnel_entry, converted=funnel_purchase,
                    rate=actual_rate * 100,
                ),
                "impact": t["impact"].format(gap=gap),
                "zone": None,
            })

    # --- Brand affinity / cross-sell ---
    total_sessions = journey.get("unique_sessions_with_zone_visits", 0)
    for aff in journey.get("brand_affinity", [])[:3]:
        if total_sessions > 0:
            pct = aff["co_visits"] / total_sessions * 100
            if pct > 25:
                t = INSIGHT_TEMPLATES["affinity_merchandising"]
                insights.append({
                    "insight_id": f"aff_{aff['zone_a']}_{aff['zone_b']}",
                    "priority": t["priority"],
                    "category": t["category"],
                    "title": t["title"],
                    "detail": t["detail"].format(
                        zone_a=aff["zone_a"], zone_b=aff["zone_b"], pct=pct
                    ),
                    "impact": t["impact"],
                    "zone": None,
                })

    # --- Peak hour staffing ---
    if revenue.get("peak_transaction_hour") is not None:
        ph = revenue["peak_transaction_hour"]
        txns_at_peak = next((tl["transactions"] for tl in revenue["hourly_timeline"] if tl["hour"] == ph), 0)
        if txns_at_peak >= 2:
            t = INSIGHT_TEMPLATES["peak_hour_staffing"]
            insights.append({
                "insight_id": f"staffing_peak_{ph}",
                "priority": t["priority"],
                "category": t["category"],
                "title": t["title"],
                "detail": t["detail"].format(hour=ph, txns=txns_at_peak, hour_end=ph + 1),
                "impact": t["impact"],
                "zone": None,
            })

    # --- Entry zone dominance ---
    entry_zones = journey.get("top_entry_zones", {})
    total_sessions = journey.get("unique_sessions_with_zone_visits", 1)
    if entry_zones and total_sessions > 5:
        top_ez = max(entry_zones, key=entry_zones.get)
        pct = entry_zones[top_ez] / total_sessions * 100
        if pct > 35:
            t = INSIGHT_TEMPLATES["first_zone_dominance"]
            insights.append({
                "insight_id": f"entry_zone_{top_ez}",
                "priority": t["priority"],
                "category": t["category"],
                "title": t["title"],
                "detail": t["detail"].format(zone=top_ez, pct=pct),
                "impact": t["impact"],
                "zone": top_ez,
            })

    # Sort by priority
    priority_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    insights.sort(key=lambda x: priority_order.get(x["priority"], 9))

    return {
        "store_id": store_id,
        "date": date_str,
        "as_of": now.isoformat().replace("+00:00", "Z"),
        "insight_count": len(insights),
        "insights": insights,
        "summary": {
            "critical": sum(1 for i in insights if i["priority"] == "CRITICAL"),
            "high": sum(1 for i in insights if i["priority"] == "HIGH"),
            "medium": sum(1 for i in insights if i["priority"] == "MEDIUM"),
            "low": sum(1 for i   in insights if i["priority"] == "LOW"),
        },
    }

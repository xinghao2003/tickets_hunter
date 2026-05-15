#!/usr/bin/env python3
#encoding=utf-8
"""platforms/goliveasia.py -- Go Live Asia platform (golive-asia.com).

Purchase flow (ticketing engine hosted on golive-asia.thaiticketmajor.com):
  1. Event detail page (golive-asia.com/event-detail/{id}/{slug})
     - Click "BUY NOW" button -> redirects to thaiticketmajor
  2. Conditions page (/booking/prww/verify_condition.php)
     - Accept T&Cs checkbox, click "Buy Ticket"
  3. Zone selection (/booking/prww/zones.php)  [Step 1/4]
     - Image map with <area> elements for each section
     - Links: fixed.php#SECTION (reserved) or festival.php#SECTION (standing)
  4. Seat selection (/booking/prww/fixed.php)  [Step 2/4]
     - HTML table grid, click available seats (id="checkseat-{ROW}-{NUM}")
     - Click "Book Now" link
  5. Attendee details (/booking/prww/enroll.php)  [Step 2/4 cont.]
     - Pre-filled form, click "Proceed to Payment Page"
  6. Payment page (external 2C2P gateway)
"""

import asyncio
import json
import random
import traceback

from zendriver import cdp

import util
from nodriver_common import (
    CONST_FROM_TOP_TO_BOTTOM,
    CONST_FROM_BOTTOM_TO_TOP,
    CONST_CENTER,
    CONST_RANDOM,
    check_and_handle_pause,
    evaluate_with_pause_check,
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
    sleep_with_pause_check,
)


__all__ = [
    "nodriver_goliveasia_main",
]

_TTM_BASE = "golive-asia.thaiticketmajor.com"

_state = {}


# ---------- golive-asia.com (marketing site) ----------

async def _goliveasia_event_detail(tab, config_dict):
    """Event detail page on golive-asia.com — click BUY NOW to enter booking."""
    debug = util.create_debug_logger(config_dict)
    debug.log("[GOLIVEASIA EVENT] Looking for BUY NOW button")

    try:
        await asyncio.sleep(random.uniform(0.5, 1.0))

        clicked = await tab.evaluate('''
            (function() {
                var btns = document.querySelectorAll('button');
                for (var i = 0; i < btns.length; i++) {
                    var txt = btns[i].textContent || '';
                    if (txt.indexOf('BUY NOW') !== -1) {
                        btns[i].click();
                        return true;
                    }
                }
                return false;
            })()
        ''')

        if clicked:
            debug.log("[GOLIVEASIA EVENT] BUY NOW clicked")
            await asyncio.sleep(random.uniform(1.0, 2.0))
            return True
        else:
            # Maybe ticket is UNAVAILABLE — check for countdown
            unavailable = await tab.evaluate('''
                (function() {
                    var btns = document.querySelectorAll('button[disabled]');
                    for (var i = 0; i < btns.length; i++) {
                        var txt = btns[i].textContent || '';
                        if (txt.indexOf('UNAVAILABLE') !== -1) return true;
                    }
                    return false;
                })()
            ''')
            if unavailable:
                debug.log("[GOLIVEASIA EVENT] Ticket UNAVAILABLE (countdown)")
            else:
                debug.log("[GOLIVEASIA EVENT] BUY NOW button not found")

    except Exception as exc:
        debug.log(f"[GOLIVEASIA EVENT] Error: {str(exc)}")

    return False


async def _goliveasia_login(tab, config_dict):
    """Login page on golive-asia.com/login — auto-fill email/password."""
    debug = util.create_debug_logger(config_dict)

    golive_account = config_dict.get("accounts", {}).get("goliveasia_account", "").strip()
    golive_password = config_dict.get("accounts", {}).get("goliveasia_password", "").strip()

    if len(golive_account) < 3 or len(golive_password) == 0:
        debug.log("[GOLIVEASIA LOGIN] No credentials configured")
        return False

    debug.log(f"[GOLIVEASIA LOGIN] Attempting login with: {golive_account[:3]}***")

    try:
        await asyncio.sleep(random.uniform(0.8, 1.2))

        # Site uses Element Plus (el-input) — inputs have dynamic IDs
        # Target by position: first input = email, second input = password
        filled = await tab.evaluate(f'''
            (function() {{
                var inputs = document.querySelectorAll('input.el-input__inner');
                var emailInput = null;
                var passwordInput = null;

                for (var i = 0; i < inputs.length; i++) {{
                    if (inputs[i].type === 'text' && !emailInput) emailInput = inputs[i];
                    if (inputs[i].type === 'password' && !passwordInput) passwordInput = inputs[i];
                }}

                if (!emailInput || !passwordInput) return 'inputs_not_found';

                // Fill email via Vue-compatible method
                emailInput.value = "{golive_account}";
                emailInput.dispatchEvent(new Event('input', {{ bubbles: true }}));

                // Fill password
                passwordInput.value = "{golive_password}";
                passwordInput.dispatchEvent(new Event('input', {{ bubbles: true }}));

                return 'ok';
            }})()
        ''')

        debug.log(f"[GOLIVEASIA LOGIN] Fill result: {filled}")

        if filled != 'ok':
            return False

        await asyncio.sleep(random.uniform(0.3, 0.6))

        # Click login button
        clicked = await tab.evaluate('''
            (function() {
                var buttons = document.querySelectorAll('button.el-button--primary.button');
                for (var i = 0; i < buttons.length; i++) {
                    if (buttons[i].textContent.trim() === 'Login') {
                        buttons[i].click();
                        return true;
                    }
                }
                return false;
            })()
        ''')

        if clicked:
            debug.log("[GOLIVEASIA LOGIN] Login button clicked, waiting for redirect...")
            for _ in range(20):
                await asyncio.sleep(0.5)
                current_url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
                if '/login' not in current_url:
                    debug.log(f"[GOLIVEASIA LOGIN] Redirected to: {current_url[:60]}...")
                    return True
            debug.log("[GOLIVEASIA LOGIN] No redirect after 10s")
        else:
            debug.log("[GOLIVEASIA LOGIN] Login button not found")

    except Exception as exc:
        debug.log(f"[GOLIVEASIA LOGIN] Error: {str(exc)}")

    return False


# ---------- golive-asia.thaiticketmajor.com (booking engine) ----------

async def _ttm_accept_conditions(tab, config_dict):
    """Conditions page — accept T&Cs and click Buy Ticket."""
    debug = util.create_debug_logger(config_dict)
    debug.log("[GOLIVEASIA TTM] On conditions page")

    try:
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Check the T&C checkbox
        checked = await tab.evaluate('''
            (function() {
                var cb = document.querySelector('input[type="checkbox"]');
                if (cb && !cb.checked) {
                    cb.click();
                    return true;
                }
                return cb ? cb.checked : false;
            })()
        ''')
        debug.log(f"[GOLIVEASIA TTM] Checkbox checked: {checked}")

        await asyncio.sleep(random.uniform(0.3, 0.5))

        # Click "Buy Ticket" button
        clicked = await tab.evaluate('''
            (function() {
                var buttons = document.querySelectorAll('button');
                for (var i = 0; i < buttons.length; i++) {
                    if (buttons[i].textContent.trim() === 'Buy Ticket') {
                        buttons[i].click();
                        return true;
                    }
                }
                return false;
            })()
        ''')

        if clicked:
            debug.log("[GOLIVEASIA TTM] Buy Ticket clicked")
            await asyncio.sleep(random.uniform(1.0, 2.0))
            return True
        else:
            debug.log("[GOLIVEASIA TTM] Buy Ticket button not found")

    except Exception as exc:
        debug.log(f"[GOLIVEASIA TTM] Error: {str(exc)}")

    return False


async def _ttm_select_zone(tab, config_dict):
    """Zone/section selection page (Step 1/4) — select area from image map."""
    debug = util.create_debug_logger(config_dict)

    area_keyword = config_dict["area_auto_select"].get("area_keyword", "").strip()
    auto_select_mode = config_dict["area_auto_select"].get("mode", CONST_FROM_TOP_TO_BOTTOM)

    debug.log(f"[GOLIVEASIA ZONE] keyword: {area_keyword}, mode: {auto_select_mode}")

    try:
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Gather all area zones from the image map
        zones_json = await tab.evaluate('''
            (function() {
                var areas = document.querySelectorAll('area');
                var zones = [];
                for (var i = 0; i < areas.length; i++) {
                    var href = areas[i].href || '';
                    var match = href.match(/#(\\w+)\\.php#(\\w+)/);
                    if (match) {
                        zones.push({
                            idx: i,
                            type: match[1],  // "fixed" or "festival"
                            section: match[2],
                            href: href
                        });
                    }
                }
                return JSON.stringify(zones);
            })()
        ''')

        zones = json.loads(zones_json) if zones_json else []
        debug.log(f"[GOLIVEASIA ZONE] Found {len(zones)} zones: {[z['section'] for z in zones]}")

        if len(zones) == 0:
            debug.log("[GOLIVEASIA ZONE] No zones found on page")
            return False

        # Filter by keyword if provided
        matched = zones
        if area_keyword:
            keywords = [kw.strip() for kw in area_keyword.split(',') if kw.strip()]
            matched = []
            for zone in zones:
                for keyword in keywords:
                    if keyword.upper() in zone['section'].upper():
                        matched.append(zone)
                        break

            if not matched:
                area_auto_fallback = config_dict.get('area_auto_fallback', False)
                if area_auto_fallback:
                    debug.log("[GOLIVEASIA ZONE] No keyword match, falling back to all zones")
                    matched = zones
                else:
                    debug.log("[GOLIVEASIA ZONE] No keyword match and fallback disabled")
                    return False

        # Pick target zone based on mode
        target = None
        if auto_select_mode == CONST_FROM_BOTTOM_TO_TOP:
            target = matched[-1] if matched else None
        elif auto_select_mode == CONST_RANDOM:
            target = random.choice(matched) if matched else None
        else:
            target = matched[0] if matched else None

        if not target:
            debug.log("[GOLIVEASIA ZONE] No target zone selected")
            return False

        debug.log(f"[GOLIVEASIA ZONE] Selecting zone: {target['section']} ({target['type']})")

        # Click the area element
        clicked = await tab.evaluate(f'''
            (function() {{
                var areas = document.querySelectorAll('area');
                if (areas[{target['idx']}]) {{
                    areas[{target['idx']}].click();
                    return true;
                }}
                return false;
            }})()
        ''')

        if clicked:
            debug.log(f"[GOLIVEASIA ZONE] Clicked zone {target['section']}")
            await asyncio.sleep(random.uniform(1.0, 2.0))
            return True
        else:
            debug.log("[GOLIVEASIA ZONE] Failed to click area element")

    except Exception as exc:
        debug.log(f"[GOLIVEASIA ZONE] Error: {str(exc)}")

    return False


async def _ttm_select_seats(tab, config_dict):
    """Seat selection page (Step 2/4) — pick available seats from grid."""
    debug = util.create_debug_logger(config_dict)
    ticket_number = config_dict.get("ticket_number", 2)

    debug.log(f"[GOLIVEASIA SEAT] Target ticket count: {ticket_number}")

    try:
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Find all available (clickable) seats
        seats_json = await tab.evaluate('''
            (function() {
                var cells = document.querySelectorAll('td[id^="checkseat-"]');
                var available = [];
                for (var i = 0; i < cells.length; i++) {
                    var cell = cells[i];
                    var style = window.getComputedStyle(cell);
                    if (style.cursor === 'pointer' || cell.getAttribute('data-available') === 'true') {
                        available.push({
                            idx: i,
                            id: cell.id,
                            text: cell.textContent.trim()
                        });
                    }
                }
                return JSON.stringify(available);
            })()
        ''')

        available_seats = json.loads(seats_json) if seats_json else []
        debug.log(f"[GOLIVEASIA SEAT] Found {len(available_seats)} available seats")

        if len(available_seats) == 0:
            debug.log("[GOLIVEASIA SEAT] No available seats")
            return False

        # Select up to ticket_number seats
        to_select = available_seats[:ticket_number]

        for seat in to_select:
            clicked = await tab.evaluate(f'''
                (function() {{
                    var el = document.getElementById("{seat['id']}");
                    if (el) {{ el.click(); return true; }}
                    return false;
                }})()
            ''')

            if clicked:
                debug.log(f"[GOLIVEASIA SEAT] Selected seat: {seat['text']}")
            else:
                debug.log(f"[GOLIVEASIA SEAT] Could not click seat: {seat['id']}")

            await asyncio.sleep(random.uniform(0.2, 0.4))

        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Click "Book Now" link
        booked = await tab.evaluate('''
            (function() {
                var links = document.querySelectorAll('a');
                for (var i = 0; i < links.length; i++) {
                    if (links[i].textContent.trim() === 'Book Now') {
                        links[i].click();
                        return true;
                    }
                }
                return false;
            })()
        ''')

        if booked:
            debug.log("[GOLIVEASIA SEAT] Book Now clicked")
            await asyncio.sleep(random.uniform(1.0, 2.0))
            return True
        else:
            debug.log("[GOLIVEASIA SEAT] Book Now link not found")

    except Exception as exc:
        debug.log(f"[GOLIVEASIA SEAT] Error: {str(exc)}")

    return False


async def _ttm_festival_select(tab, config_dict):
    """Festival/standing section — select quantity (no individual seats)."""
    debug = util.create_debug_logger(config_dict)
    ticket_number = config_dict.get("ticket_number", 2)

    debug.log(f"[GOLIVEASIA FESTIVAL] Target quantity: {ticket_number}")

    try:
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Festival sections typically have a quantity selector
        result = await tab.evaluate(f'''
            (function() {{
                // Look for quantity input or +/- buttons
                var qtyInput = document.querySelector('input[type="number"], input[name="quantity"]');
                if (qtyInput) {{
                    qtyInput.value = "{ticket_number}";
                    qtyInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return 'quantity_set';
                }}

                // Look for +/- buttons to click
                var plusBtn = document.querySelector('.btn-plus, [data-action="plus"], button.qty-plus');
                if (plusBtn) {{
                    for (var i = 1; i < {ticket_number}; i++) {{
                        plusBtn.click();
                    }}
                    return 'plus_clicked';
                }}

                return 'no_quantity_control';
            }})()
        ''')

        debug.log(f"[GOLIVEASIA FESTIVAL] Quantity result: {result}")

        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Click Book Now
        booked = await tab.evaluate('''
            (function() {
                var links = document.querySelectorAll('a');
                for (var i = 0; i < links.length; i++) {
                    if (links[i].textContent.trim() === 'Book Now') {
                        links[i].click();
                        return true;
                    }
                }
                return false;
            })()
        ''')

        if booked:
            debug.log("[GOLIVEASIA FESTIVAL] Book Now clicked")
            await asyncio.sleep(random.uniform(1.0, 2.0))
            return True

    except Exception as exc:
        debug.log(f"[GOLIVEASIA FESTIVAL] Error: {str(exc)}")

    return False


async def _ttm_enroll(tab, config_dict):
    """Enrollment/details page — submit pre-filled attendee form."""
    debug = util.create_debug_logger(config_dict)
    debug.log("[GOLIVEASIA ENROLL] On attendee details page")

    try:
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # The form is pre-filled from account data — just click Proceed
        clicked = await tab.evaluate('''
            (function() {
                var buttons = document.querySelectorAll('button');
                for (var i = 0; i < buttons.length; i++) {
                    var txt = buttons[i].textContent.trim();
                    if (txt.indexOf('Proceed') !== -1 || txt.indexOf('Payment') !== -1) {
                        buttons[i].click();
                        return txt;
                    }
                }
                return false;
            })()
        ''')

        if clicked:
            debug.log(f"[GOLIVEASIA ENROLL] Clicked: {clicked}")
            await asyncio.sleep(random.uniform(1.0, 2.0))
            return True
        else:
            debug.log("[GOLIVEASIA ENROLL] Proceed button not found")

    except Exception as exc:
        debug.log(f"[GOLIVEASIA ENROLL] Error: {str(exc)}")

    return False


# ---------- Main router ----------

async def nodriver_goliveasia_main(tab, url, config_dict):
    """Go Live Asia main function — routes based on URL patterns.

    Handles two domains:
      - golive-asia.com: marketing site (event detail, login)
      - golive-asia.thaiticketmajor.com: booking engine (zones, seats, payment)
    """
    if not _state:
        _state.update({
            "fail_list": [],
            "last_activity": "",
            "purchase_logged": False,
        })

    debug = util.create_debug_logger(config_dict)
    debug.log(f"[GOLIVEASIA MAIN] URL: {url[:80]}...")

    result = False

    try:
        # ===== Payment / success detection =====
        if '/payment' in url and _TTM_BASE not in url:
            # External payment gateway
            if not _state.get("payment_logged", False):
                print("[GOLIVEASIA] Payment page reached!")
                play_sound_while_ordering(config_dict)
                send_discord_notification(config_dict, "payment", "goliveasia")
                send_telegram_notification(config_dict, "payment", "goliveasia")
                _state["payment_logged"] = True
            return True

        # ===== golive-asia.com pages =====
        if _TTM_BASE not in url:
            if '/login' in url:
                result = await _goliveasia_login(tab, config_dict)

            elif '/event-detail/' in url:
                _state["last_activity"] = url
                result = await _goliveasia_event_detail(tab, config_dict)

            elif '/home' in url or url.endswith('.com/') or url.endswith('.com'):
                # Homepage — check if we should redirect to a specific event
                homepage = config_dict.get("homepage", "")
                if homepage and '/event-detail/' in homepage:
                    debug.log(f"[GOLIVEASIA MAIN] Redirecting to event: {homepage[:60]}...")
                    await tab.get(homepage)
                    result = True

            return result

        # ===== golive-asia.thaiticketmajor.com pages =====

        if 'verify_condition' in url:
            # Conditions page — accept T&Cs
            result = await _ttm_accept_conditions(tab, config_dict)

        elif 'zones.php' in url:
            # Step 1/4: Zone/section selection
            result = await _ttm_select_zone(tab, config_dict)

        elif 'fixed.php' in url:
            # Step 2/4: Fixed/reserved seat selection
            result = await _ttm_select_seats(tab, config_dict)

        elif 'festival.php' in url:
            # Step 2/4: Festival/standing — quantity selection
            result = await _ttm_festival_select(tab, config_dict)

        elif 'enroll.php' in url:
            # Attendee details — proceed to payment
            result = await _ttm_enroll(tab, config_dict)

        elif 'payment' in url or 'checkout' in url or 'confirm' in url:
            if not _state.get("payment_logged", False):
                print("[GOLIVEASIA] Payment/checkout page reached!")
                play_sound_while_ordering(config_dict)
                send_discord_notification(config_dict, "payment", "goliveasia")
                send_telegram_notification(config_dict, "payment", "goliveasia")
                _state["payment_logged"] = True
            result = True

        else:
            debug.log(f"[GOLIVEASIA TTM] Unrecognized booking page: {url[:60]}...")

    except Exception as exc:
        debug.log(f"[GOLIVEASIA MAIN] Error: {str(exc)}")
        debug.log(traceback.format_exc())

    return result

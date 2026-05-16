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
import urllib.parse

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
_GOLIVE_LOGIN_URL = "https://www.golive-asia.com/login"

_state = {}


def _get_current_url(tab):
    return tab.url if hasattr(tab, 'url') else str(tab.target.url)


def _is_event_or_sales_url(url):
    return '/event-detail/' in url or '/sale' in url


def _remember_event_url(url):
    if _is_event_or_sales_url(url):
        _state["pending_event_url"] = url


def _ordered_zones(zones, mode):
    if mode == CONST_FROM_BOTTOM_TO_TOP:
        return list(reversed(zones))
    if mode == CONST_RANDOM:
        shuffled = list(zones)
        random.shuffle(shuffled)
        return shuffled
    return zones


def _get_section_from_url(url):
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("zone", "section", "zoneName"):
        values = query.get(key)
        if values and values[0]:
            return values[0]
    return parsed.fragment


def _mark_current_zone_failed(url, debug, reason):
    section = _state.get("current_zone", "") or _get_section_from_url(url)
    if section:
        fail_list = _state.setdefault("fail_list", [])
        if section not in fail_list:
            fail_list.append(section)
        debug.log(f"[GOLIVEASIA SEAT] {reason} in {section}; marked failed")
    else:
        debug.log(f"[GOLIVEASIA SEAT] {reason}; section unknown")


async def _ttm_back_to_zones(tab, config_dict):
    debug = util.create_debug_logger(config_dict)

    try:
        zones_url = _state.get("last_zones_url", "")
        if zones_url:
            debug.log(f"[GOLIVEASIA SEAT] Returning to zones page: {zones_url[:80]}...")
            await tab.get(zones_url)
        else:
            debug.log("[GOLIVEASIA SEAT] No stored zones URL; using browser history")
            await tab.evaluate("history.back()")

        await asyncio.sleep(random.uniform(1.0, 2.0))
    except Exception as exc:
        debug.log(f"[GOLIVEASIA SEAT] Failed to return to zones page: {str(exc)}")


async def _goto_login(tab, config_dict, source_url=None):
    debug = util.create_debug_logger(config_dict)

    if source_url:
        _remember_event_url(source_url)

    debug.log("[GOLIVEASIA LOGIN] Navigating to login page")
    await tab.get(_GOLIVE_LOGIN_URL)
    await asyncio.sleep(random.uniform(1.0, 2.0))


async def _handle_login_modal(tab, config_dict):
    """Detect and handle the 'Sign In to Proceed' overlay modal after BUY NOW click."""
    debug = util.create_debug_logger(config_dict)

    try:
        source_url = _get_current_url(tab)
        modal_result = await tab.evaluate('''
            (function() {
                // Check for the login dialog overlay
                var dialogs = document.querySelectorAll('[class*="dialog"], [class*="modal"], [role="dialog"]');
                for (var i = 0; i < dialogs.length; i++) {
                    var txt = dialogs[i].textContent || '';
                    if (txt.indexOf('Sign In') !== -1 || txt.indexOf('GO TO LOGIN') !== -1) {
                        // Close the modal if possible, then navigate directly to /login.
                        var btns = dialogs[i].querySelectorAll('button');
                        for (var j = 0; j < btns.length; j++) {
                            if (btns[j].textContent.trim() === 'CANCEL') {
                                btns[j].click();
                                return 'login_modal_cancel';
                            }
                        }
                        return 'login_modal_found';
                    }
                }
                return null;
            })()
        ''')

        if modal_result:
            debug.log(f"[GOLIVEASIA MODAL] {modal_result}")
            await asyncio.sleep(random.uniform(0.5, 1.0))

            await _goto_login(tab, config_dict, source_url)
            return True

    except Exception as exc:
        debug.log(f"[GOLIVEASIA MODAL] Error: {str(exc)}")

    return False


async def _check_logged_in(tab):
    """Check if user is logged in on golive-asia.com."""
    try:
        result = await tab.evaluate('''
            (function() {
                var body = document.body.innerText || '';
                if (body.indexOf('Hi,') !== -1 || body.indexOf('Logout') !== -1) return true;

                var loggedOutTextFound = false;
                var userElementFound = false;
                var els = document.querySelectorAll('a, button, [class*="login"], [class*="user"], [class*="avatar"]');
                for (var i = 0; i < els.length; i++) {
                    var txt = els[i].textContent || '';
                    var normalized = txt.trim().toLowerCase();
                    if (normalized === 'login' || normalized === 'sign in') loggedOutTextFound = true;
                    if (normalized.indexOf('logout') !== -1 || normalized.indexOf('my account') !== -1 || normalized.indexOf('profile') !== -1) return true;
                    if (els[i].className && String(els[i].className).match(/user|avatar/i)) userElementFound = true;
                }

                if (loggedOutTextFound) return false;
                if (userElementFound) return true;
                return null;
            })()
        ''')
        if result is None:
            return _state.get("login_completed", False)
        return result
    except Exception:
        return _state.get("login_completed", False)


# ---------- golive-asia.com (marketing site) ----------

async def _goliveasia_event_detail(tab, config_dict):
    """Event detail page on golive-asia.com — click BUY NOW to enter booking."""
    debug = util.create_debug_logger(config_dict)

    # Guard: don't keep clicking BUY NOW in a loop
    if _state.get("buy_now_clicked", False):
        # Check if a login modal appeared
        modal_handled = await _handle_login_modal(tab, config_dict)
        if modal_handled:
            return True

        # Check if we've navigated away (to login or thaiticketmajor)
        current_url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
        if 'thaiticketmajor' in current_url or '/login' in current_url:
            return True

        # Still on event page after click — wait for navigation
        debug.log("[GOLIVEASIA EVENT] Waiting for navigation after BUY NOW...")
        return False

    debug.log("[GOLIVEASIA EVENT] Looking for BUY NOW button")

    try:
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Check if logged in first
        is_logged_in = await _check_logged_in(tab)
        if not is_logged_in:
            debug.log("[GOLIVEASIA EVENT] Not logged in — redirecting to login")
            current_url = _get_current_url(tab)
            await _goto_login(tab, config_dict, current_url)
            return True

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
            _state["buy_now_clicked"] = True
            await asyncio.sleep(random.uniform(1.0, 2.0))

            # Check if login modal appeared instead of redirect
            modal_handled = await _handle_login_modal(tab, config_dict)
            if modal_handled:
                return True

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
                    _state["login_completed"] = True
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

        fail_list = _state.setdefault("fail_list", [])
        if fail_list:
            matched = [zone for zone in matched if zone["section"] not in fail_list]
            debug.log(f"[GOLIVEASIA ZONE] Skipping failed zones: {fail_list}")

        if len(matched) == 0:
            debug.log("[GOLIVEASIA ZONE] No untried matching zones")
            return False

        # Pick target zone based on mode
        ordered = _ordered_zones(matched, auto_select_mode)
        target = ordered[0] if ordered else None

        if not target:
            debug.log("[GOLIVEASIA ZONE] No target zone selected")
            return False

        debug.log(f"[GOLIVEASIA ZONE] Selecting zone: {target['section']} ({target['type']})")
        _state["current_zone"] = target["section"]

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
            current_url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
            _mark_current_zone_failed(current_url, debug, "No available seats")

            await _ttm_back_to_zones(tab, config_dict)
            return True

        if len(available_seats) < ticket_number:
            current_url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
            _mark_current_zone_failed(
                current_url,
                debug,
                f"Only {len(available_seats)} available seats for requested {ticket_number}"
            )

            await _ttm_back_to_zones(tab, config_dict)
            return True

        # Select up to ticket_number seats
        to_select = available_seats[:ticket_number]
        selected_count = 0

        for seat in to_select:
            clicked = await tab.evaluate(f'''
                (function() {{
                    var el = document.getElementById("{seat['id']}");
                    if (el) {{ el.click(); return true; }}
                    return false;
                }})()
            ''')

            if clicked:
                selected_count += 1
                debug.log(f"[GOLIVEASIA SEAT] Selected seat: {seat['text']}")
            else:
                debug.log(f"[GOLIVEASIA SEAT] Could not click seat: {seat['id']}")

            await asyncio.sleep(random.uniform(0.2, 0.4))

        if selected_count < ticket_number:
            current_url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
            _mark_current_zone_failed(
                current_url,
                debug,
                f"Selected {selected_count} seats for requested {ticket_number}"
            )

            await _ttm_back_to_zones(tab, config_dict)
            return True

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
            current_url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
            if 'fixed.php' in current_url:
                _mark_current_zone_failed(current_url, debug, "Book Now did not advance")
                await _ttm_back_to_zones(tab, config_dict)
            return True
        else:
            debug.log("[GOLIVEASIA SEAT] Book Now link not found")
            current_url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
            _mark_current_zone_failed(current_url, debug, "Book Now link not found")
            await _ttm_back_to_zones(tab, config_dict)
            return True

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
                function fireChange(el) {{
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}

                var soldOutText = (document.body.innerText || '').toLowerCase();
                if (soldOutText.indexOf('sold out') !== -1 || soldOutText.indexOf('unavailable') !== -1) {{
                    return 'sold_out';
                }}

                // Look for quantity input.
                var qtyInput = document.querySelector(
                    'input[type="number"], input[name*="qty" i], input[name*="quantity" i], input[id*="qty" i], input[id*="quantity" i]'
                );
                if (qtyInput) {{
                    qtyInput.value = "{ticket_number}";
                    fireChange(qtyInput);
                    return 'quantity_set';
                }}

                // Some standing pages use a select dropdown for quantity.
                var selects = document.querySelectorAll('select');
                for (var i = 0; i < selects.length; i++) {{
                    var select = selects[i];
                    for (var j = 0; j < select.options.length; j++) {{
                        if (select.options[j].value === "{ticket_number}" || select.options[j].text.trim() === "{ticket_number}") {{
                            select.selectedIndex = j;
                            fireChange(select);
                            return 'select_set';
                        }}
                    }}
                }}

                // Look for +/- controls. For target 1, click plus once from zero.
                var plusBtn = document.querySelector(
                    '.btn-plus, .qty-plus, [data-action="plus"], [aria-label*="plus" i], [aria-label*="increase" i], button[class*="plus" i], a[class*="plus" i]'
                );
                if (plusBtn) {{
                    for (var i = 0; i < {ticket_number}; i++) {{
                        plusBtn.click();
                    }}
                    return 'plus_clicked';
                }}

                return 'no_quantity_control';
            }})()
        ''')

        debug.log(f"[GOLIVEASIA FESTIVAL] Quantity result: {result}")

        if result in ('no_quantity_control', 'sold_out'):
            current_url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
            _mark_current_zone_failed(current_url, debug, f"Festival quantity result {result}")
            await _ttm_back_to_zones(tab, config_dict)
            return True

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
            current_url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
            if 'festival.php' in current_url:
                _mark_current_zone_failed(current_url, debug, "Festival Book Now did not advance")
                await _ttm_back_to_zones(tab, config_dict)
            return True
        else:
            current_url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
            _mark_current_zone_failed(current_url, debug, "Festival Book Now link not found")
            await _ttm_back_to_zones(tab, config_dict)
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
                _state["buy_now_clicked"] = False
                result = await _goliveasia_login(tab, config_dict)

            elif '/event-detail/' in url:
                _state["last_activity"] = url
                result = await _goliveasia_event_detail(tab, config_dict)

            elif '/home' in url or url.endswith('.com/') or url.endswith('.com'):
                # Homepage — check if we should redirect to a specific event
                target_url = _state.pop("pending_event_url", "") or config_dict.get("homepage", "")
                if target_url and _is_event_or_sales_url(target_url):
                    debug.log(f"[GOLIVEASIA MAIN] Redirecting to event: {target_url[:60]}...")
                    await tab.get(target_url)
                    result = True

            return result

        # ===== golive-asia.thaiticketmajor.com pages =====
        _state["buy_now_clicked"] = False

        if 'verify_condition' in url:
            # Conditions page — accept T&Cs
            _state["fail_list"] = []
            _state["current_zone"] = ""
            result = await _ttm_accept_conditions(tab, config_dict)

        elif 'zones.php' in url:
            # Step 1/4: Zone/section selection
            _state["last_zones_url"] = url
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

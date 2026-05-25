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


def _is_excluded_by_keyword(config_dict, row_text):
    keyword_exclude = config_dict.get("keyword_exclude", "")
    if keyword_exclude and row_text:
        return util.reset_row_text_if_match_keyword_exclude(config_dict, row_text)
    return False


def _seat_sort_key(seat):
    row = seat.get("row", "")
    number = seat.get("number")
    if number is None:
        number = 999999
    return (row, number)


def _select_target_seats(available_seats, ticket_number, allow_non_adjacent):
    if allow_non_adjacent or ticket_number <= 1:
        return available_seats[:ticket_number]

    seats_by_row = {}
    for seat in available_seats:
        row = seat.get("row", "")
        number = seat.get("number")
        if row and number is not None:
            seats_by_row.setdefault(row, []).append(seat)

    for row in sorted(seats_by_row.keys()):
        row_seats = sorted(seats_by_row[row], key=lambda item: item["number"])
        for start_index in range(0, len(row_seats)):
            block = [row_seats[start_index]]
            last_number = row_seats[start_index]["number"]
            for seat in row_seats[start_index + 1:]:
                if seat["number"] == last_number + 1:
                    block.append(seat)
                    last_number = seat["number"]
                    if len(block) >= ticket_number:
                        return block[:ticket_number]
                elif seat["number"] > last_number + 1:
                    break

    return []


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


async def _handle_buy_now_dropdown(tab, config_dict):
    """Click a visible sale option when BUY NOW opens a dropdown menu."""
    debug = util.create_debug_logger(config_dict)

    try:
        result = await tab.evaluate('''
            (function() {
                function isVisible(el) {
                    if (!el) return false;
                    var rect = el.getBoundingClientRect();
                    var style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.opacity !== '0';
                }

                var items = [];
                var candidates = document.querySelectorAll(
                    '[role="menuitem"], .el-dropdown-menu__item, li[class*="dropdown" i], a[class*="dropdown" i]'
                );
                for (var i = 0; i < candidates.length; i++) {
                    var el = candidates[i];
                    var text = (el.textContent || '').trim().replace(/\\s+/g, ' ');
                    if (!text || !isVisible(el)) continue;
                    items.push({ idx: i, text: text, score: 0 });
                }

                if (items.length === 0) return JSON.stringify({ clicked: false, reason: 'no_visible_items' });

                for (var j = 0; j < items.length; j++) {
                    var lower = items[j].text.toLowerCase();
                    if (lower.indexOf('general sales') !== -1 || lower.indexOf('general sale') !== -1) {
                        items[j].score += 100;
                    }
                    if (lower.indexOf('vip') !== -1 || lower.indexOf('upgrade') !== -1) {
                        items[j].score -= 25;
                    }
                    if (lower.indexOf('sold out') !== -1 || lower.indexOf('unavailable') !== -1) {
                        items[j].score -= 100;
                    }
                }

                items.sort(function(a, b) {
                    if (b.score !== a.score) return b.score - a.score;
                    return a.idx - b.idx;
                });

                var targetInfo = items[0];
                var target = candidates[targetInfo.idx];
                target.click();
                return JSON.stringify({
                    clicked: true,
                    text: targetInfo.text,
                    score: targetInfo.score
                });
            })()
        ''')

        data = json.loads(result) if result else {}
        if data.get("clicked"):
            debug.log(f"[GOLIVEASIA EVENT] Sale option clicked: {data.get('text', '')}")
            await asyncio.sleep(random.uniform(1.0, 2.0))
            return True

    except Exception as exc:
        debug.log(f"[GOLIVEASIA EVENT] Sale dropdown error: {str(exc)}")

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
        dropdown_handled = await _handle_buy_now_dropdown(tab, config_dict)
        if dropdown_handled:
            return True

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

            dropdown_handled = await _handle_buy_now_dropdown(tab, config_dict)
            if dropdown_handled:
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
                    target_url = _state.pop("pending_event_url", "") or config_dict.get("homepage", "")
                    if target_url and _is_event_or_sales_url(target_url) and target_url not in current_url:
                        debug.log(f"[GOLIVEASIA LOGIN] Navigating to target event: {target_url[:60]}...")
                        await tab.get(target_url)
                        await asyncio.sleep(random.uniform(1.0, 2.0))
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
                    var txt = buttons[i].textContent.trim().toLowerCase();
                    if (txt === 'buy ticket') {
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


async def _ttm_select_date(tab, config_dict):
    """Zones page date selector — choose #rdId before selecting a section."""
    debug = util.create_debug_logger(config_dict)

    date_auto_select = config_dict.get("date_auto_select", {})
    auto_select_mode = date_auto_select.get("mode", CONST_FROM_TOP_TO_BOTTOM)
    date_keyword = date_auto_select.get("date_keyword", "").strip()
    date_auto_fallback = config_dict.get("date_auto_fallback", False)

    try:
        await asyncio.sleep(random.uniform(0.3, 0.6))

        dates_json = await tab.evaluate('''
            (function() {
                var select = document.querySelector('select#rdId, select[name="rdId"]');
                if (!select) return JSON.stringify({ found: false, current: "", options: [] });

                var options = [];
                for (var i = 0; i < select.options.length; i++) {
                    var opt = select.options[i];
                    var text = (opt.textContent || '').trim();
                    var value = opt.value || '';
                    if (value && !opt.disabled) {
                        options.push({
                            idx: i,
                            text: text,
                            value: value,
                            selected: opt.selected
                        });
                    }
                }

                return JSON.stringify({
                    found: true,
                    current: select.value || "",
                    options: options
                });
            })()
        ''')

        date_info = json.loads(dates_json) if dates_json else {}
        if not date_info.get("found"):
            return True

        current_round = date_info.get("current", "")
        if current_round:
            _state["selected_round"] = current_round
            debug.log(f"[GOLIVEASIA DATE] Round already selected: {current_round}")
            return True

        if not date_auto_select.get("enable", True):
            debug.log("[GOLIVEASIA DATE] Date auto-select disabled, waiting for manual date selection")
            return False

        date_options = date_info.get("options", [])
        debug.log(f"[GOLIVEASIA DATE] Found {len(date_options)} date options")
        if len(date_options) == 0:
            debug.log("[GOLIVEASIA DATE] No selectable dates")
            return False

        matched_dates = []
        if len(date_keyword) == 0:
            matched_dates = date_options
        else:
            debug.log(f"[GOLIVEASIA DATE] Matching keyword: {date_keyword}")
            for date_item in date_options:
                row_text = date_item.get("text", "")
                debug.log(f"[GOLIVEASIA DATE] row_text: {row_text}")
                if util.is_row_match_keyword(date_keyword, row_text):
                    matched_dates.append(date_item)
                    if auto_select_mode == CONST_FROM_TOP_TO_BOTTOM:
                        break

        debug.log(f"[GOLIVEASIA DATE] Matched {len(matched_dates)} dates")

        if len(matched_dates) == 0:
            if date_auto_fallback:
                matched_dates = date_options
                debug.log("[GOLIVEASIA DATE] date_auto_fallback=true, selecting from all dates")
            else:
                debug.log("[GOLIVEASIA DATE] date_auto_fallback=false, waiting for manual date selection")
                return False

        target_date = util.get_target_item_from_matched_list(matched_dates, auto_select_mode)
        if not target_date:
            debug.log("[GOLIVEASIA DATE] No target date selected")
            return False

        target_value = target_date.get("value", "")
        target_text = target_date.get("text", "")
        debug.log(f"[GOLIVEASIA DATE] Selecting date: {target_text} ({target_value})")

        selected = await tab.evaluate(f'''
            (function() {{
                var select = document.querySelector('select#rdId, select[name="rdId"]');
                if (!select) return false;
                select.value = {json.dumps(target_value)};
                select.dispatchEvent(new Event('input', {{ bubbles: true }}));
                select.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return select.value === {json.dumps(target_value)};
            }})()
        ''')

        if selected:
            _state["selected_round"] = target_value
            await asyncio.sleep(random.uniform(1.0, 1.5))
            return True

        debug.log("[GOLIVEASIA DATE] Failed to set date select value")

    except Exception as exc:
        debug.log(f"[GOLIVEASIA DATE] Error: {str(exc)}")

    return False


async def _ttm_get_available_zones(tab, config_dict):
    """Read the Seats Available popup data without opening the popup."""
    debug = util.create_debug_logger(config_dict)

    try:
        availability_json = await tab.evaluate('''
            (async function() {
                var select = document.querySelector('select#rdId, select[name="rdId"]');
                var round = select ? select.value : '';
                if (!round) {
                    var params = new URLSearchParams(location.search);
                    round = params.get('rdId') || params.get('round') || '';
                }
                if (!round) return JSON.stringify([]);

                try {
                    var response = await fetch('zonesavail.php?round=' + encodeURIComponent(round), {
                        credentials: 'include'
                    });
                    var html = await response.text();
                    var doc = new DOMParser().parseFromString(html, 'text/html');
                    var rows = doc.querySelectorAll('#avail_data tbody tr, table tbody tr');
                    var zones = [];

                    for (var i = 0; i < rows.length; i++) {
                        var row = rows[i];
                        var cells = row.querySelectorAll('td');
                        if (cells.length < 2) continue;

                        var sectionText = (cells[0].textContent || '').trim();
                        var statusText = (cells[1].textContent || '').trim();
                        var onclick = row.getAttribute('onclick') || '';
                        var section = '';
                        var type = 'fixed';

                        var link = cells[0].querySelector('a[id]');
                        if (link && link.id) section = link.id;

                        var match = onclick.match(/gonextstep\\(['"]([^'"]+)['"]\\s*,\\s*['"]([^'"]+)['"]/);
                        if (match) {
                            type = match[1].replace(/\\.php$/, '');
                            section = match[2];
                        }

                        if (!section) {
                            section = sectionText.split(/\\s+/)[0];
                        }

                        if (section && /available/i.test(statusText)) {
                            zones.push({
                                type: type,
                                section: section,
                                text: sectionText,
                                status: statusText,
                                idx: i
                            });
                        }
                    }

                    return JSON.stringify(zones);
                } catch (err) {
                    return JSON.stringify([]);
                }
            })()
        ''')

        available_zones = json.loads(availability_json) if availability_json else []
        if available_zones:
            debug.log(
                f"[GOLIVEASIA AVAIL] Found available zones: "
                f"{[zone.get('text') or zone.get('section') for zone in available_zones]}"
            )
        else:
            debug.log("[GOLIVEASIA AVAIL] No available-zone rows found")
        return available_zones

    except Exception as exc:
        debug.log(f"[GOLIVEASIA AVAIL] Error: {str(exc)}")

    return []


async def _ttm_select_zone(tab, config_dict):
    """Zone/section selection page (Step 1/4) — select area from image map."""
    debug = util.create_debug_logger(config_dict)

    area_auto_select = config_dict.get("area_auto_select", {})
    area_keyword = area_auto_select.get("area_keyword", "").strip()
    auto_select_mode = area_auto_select.get("mode", CONST_FROM_TOP_TO_BOTTOM)

    debug.log(f"[GOLIVEASIA ZONE] keyword: {area_keyword}, mode: {auto_select_mode}")

    try:
        await asyncio.sleep(random.uniform(0.5, 1.0))

        if not area_auto_select.get("enable", True):
            debug.log("[GOLIVEASIA ZONE] Area auto-select disabled, waiting for manual section selection")
            return False

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

        available_zones = await _ttm_get_available_zones(tab, config_dict)
        if available_zones:
            zones_by_section = {zone["section"]: zone for zone in zones}
            prioritized_zones = []
            for available_zone in available_zones:
                section = available_zone.get("section", "")
                zone = zones_by_section.get(section)
                if zone:
                    merged_zone = dict(zone)
                    merged_zone["availability_text"] = available_zone.get("text", "")
                    merged_zone["availability_status"] = available_zone.get("status", "")
                    prioritized_zones.append(merged_zone)

            if prioritized_zones:
                debug.log(
                    f"[GOLIVEASIA ZONE] Prioritizing available zones: "
                    f"{[zone.get('availability_text') or zone['section'] for zone in prioritized_zones]}"
                )
                zones = prioritized_zones
            else:
                debug.log("[GOLIVEASIA ZONE] Available rows did not match image-map zones; using image map")

        filtered_zones = []
        for zone in zones:
            row_text = " ".join([
                zone.get("section", ""),
                zone.get("availability_text", ""),
                zone.get("availability_status", ""),
            ]).strip()
            if _is_excluded_by_keyword(config_dict, row_text):
                debug.log(f"[GOLIVEASIA ZONE] Excluded by keyword_exclude: {row_text}")
                continue
            filtered_zones.append(zone)

        zones = filtered_zones
        if len(zones) == 0:
            debug.log("[GOLIVEASIA ZONE] All zones excluded by keyword_exclude")
            return False

        # Filter by keyword if provided
        matched = zones
        if area_keyword:
            matched = []
            for zone in zones:
                row_text = " ".join([
                    zone.get("section", ""),
                    zone.get("availability_text", ""),
                    zone.get("availability_status", ""),
                ]).strip()
                is_match_area = util.is_row_match_keyword(area_keyword, row_text)
                if not is_match_area:
                    keywords = [kw.strip() for kw in area_keyword.split(',') if kw.strip()]
                    for keyword in keywords:
                        if keyword.upper() in row_text.upper():
                            is_match_area = True
                            break

                if is_match_area:
                    matched.append(zone)

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
    allow_non_adjacent = config_dict.get("advanced", {}).get("disable_adjacent_seat", False)

    debug.log(f"[GOLIVEASIA SEAT] Target ticket count: {ticket_number}")
    debug.log(f"[GOLIVEASIA SEAT] Allow non-adjacent seats: {allow_non_adjacent}")

    try:
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Find all available (clickable) seats
        seats_json = await tab.evaluate('''
            (function() {
                function parseSeat(raw) {
                    raw = raw || '';
                    var match = raw.match(/^([A-Za-z]+)-0*(\\d+)/);
                    if (!match) match = raw.match(/^([A-Za-z]+)\\s*0*(\\d+)/);
                    if (!match) return { row: '', number: null };
                    return {
                        row: match[1],
                        number: parseInt(match[2], 10)
                    };
                }

                var cells = document.querySelectorAll(
                    'td[id^="checkseat-"], div[id^="checkseat-"].seatuncheck, div[id^="checkseat-"][data-seat]'
                );
                var available = [];
                for (var i = 0; i < cells.length; i++) {
                    var cell = cells[i];
                    var style = window.getComputedStyle(cell);
                    var isSeatUnavailable = (
                        cell.classList.contains('seatnotavail') ||
                        cell.classList.contains('not-available') ||
                        cell.closest('.not-available')
                    );
                    if (!isSeatUnavailable && (
                        style.cursor === 'pointer' ||
                        cell.classList.contains('seatuncheck') ||
                        cell.getAttribute('data-seat') ||
                        cell.getAttribute('data-available') === 'true'
                    )) {
                        var rawSeat = cell.getAttribute('data-seat') ||
                            cell.getAttribute('title') ||
                            (cell.closest('td') ? cell.closest('td').getAttribute('title') : '') ||
                            cell.id.replace(/^checkseat-/, '') ||
                            cell.textContent.trim();
                        var parsedSeat = parseSeat(rawSeat);
                        available.push({
                            idx: i,
                            id: cell.id,
                            text: rawSeat,
                            row: parsedSeat.row,
                            number: parsedSeat.number
                        });
                    }
                }
                return JSON.stringify(available);
            })()
        ''')

        available_seats = json.loads(seats_json) if seats_json else []
        available_seats = sorted(available_seats, key=_seat_sort_key)
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
        to_select = _select_target_seats(available_seats, ticket_number, allow_non_adjacent)
        if len(to_select) < ticket_number:
            current_url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
            reason = f"No adjacent block for requested {ticket_number}"
            if allow_non_adjacent:
                reason = f"Only {len(to_select)} selectable seats for requested {ticket_number}"
            _mark_current_zone_failed(current_url, debug, reason)

            await _ttm_back_to_zones(tab, config_dict)
            return True

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


def _notify_order_reached(config_dict, message="[GOLIVEASIA] Order page reached!"):
    if _state.get("payment_logged", False):
        return

    print(message)
    play_sound_while_ordering(config_dict)
    send_discord_notification(config_dict, "order", "goliveasia")
    send_telegram_notification(config_dict, "order", "goliveasia")
    _state["payment_logged"] = True


async def _ttm_enroll(tab, config_dict):
    """Enrollment/details page — submit pre-filled attendee form."""
    debug = util.create_debug_logger(config_dict)
    debug.log("[GOLIVEASIA ENROLL] On attendee details page")
    _notify_order_reached(config_dict)

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
            _notify_order_reached(config_dict, "[GOLIVEASIA] Payment page reached!")
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
            is_date_ready = await _ttm_select_date(tab, config_dict)
            if is_date_ready:
                current_url = _get_current_url(tab)
                if current_url != url:
                    _state["last_zones_url"] = current_url
                    result = True
                else:
                    result = await _ttm_select_zone(tab, config_dict)
            else:
                result = False

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
            _notify_order_reached(config_dict, "[GOLIVEASIA] Payment/checkout page reached!")
            result = True

        else:
            debug.log(f"[GOLIVEASIA TTM] Unrecognized booking page: {url[:60]}...")

    except Exception as exc:
        debug.log(f"[GOLIVEASIA MAIN] Error: {str(exc)}")
        debug.log(traceback.format_exc())

    return result

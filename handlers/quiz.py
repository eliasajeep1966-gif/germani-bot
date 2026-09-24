import html
import os
import random
import asyncio
from aiogram import Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from utils import render_progress_bar, get_content, resolve_filename
from database import (
    QuizState, Teil3State, AuthState,
    can_access_level, mark_text_completed, is_text_completed,
    record_answer_stat, is_free_content
)
from keyboards import get_quiz_navigation_keyboard, get_cancel_to_main_keyboard, get_paywall_keyboard, get_dynamic_paywall_text

quiz_router = Router()

def _extract_quiz_content(content, file_name: str) -> dict:
    """
    State Pointers refactor: derive the quiz view-model from cached content
    on demand instead of storing blobs in FSM. Never mutates the cached object:
    question dicts are shallow-copied before merging global options.
    """
    clean_title_fallback = file_name.replace('.json', '').replace('_', ' ').title()
    parsed = {
        "questions": [],
        "title": clean_title_fallback,
        "summary": "",
        "fixed_question": "",
        "text_body": "",
        "keywords": "",
    }
    questions_list = []
    global_options = []

    if isinstance(content, list):
        questions_list = list(content)
    elif isinstance(content, dict):
        parsed["title"] = content.get('title', clean_title_fallback)
        parsed["summary"] = content.get('summary', '')
        parsed["fixed_question"] = content.get('fixed_question', '')
        parsed["text_body"] = content.get('text') or content.get('body') or ''
        global_options = content.get('options', [])

        kw = content.get('keywords') or content.get('end_of_text_summary', {}).get('keywords', '')
        if isinstance(kw, list):
            parsed["keywords"] = ", ".join([str(k) for k in kw])
        elif isinstance(kw, dict):
            parsed["keywords"] = ", ".join([f"{k}: {v}" for k, v in kw.items()])
        else:
            parsed["keywords"] = str(kw)

        if 'questions' in content and isinstance(content['questions'], list):
            questions_list = list(content['questions'])
        elif 'audios' in content and isinstance(content['audios'], list):
            for audio_item in content['audios']:
                if isinstance(audio_item, dict) and 'questions' in audio_item and isinstance(audio_item['questions'], list):
                    questions_list.extend(audio_item['questions'])

    if global_options:
        merged = []
        for q_item in questions_list:
            if isinstance(q_item, dict) and 'options' not in q_item:
                q_copy = dict(q_item)
                q_copy['options'] = global_options
                merged.append(q_copy)
            else:
                merged.append(q_item)
        questions_list = merged

    parsed["questions"] = questions_list
    return parsed


def _build_question_order(skill: str, teil: str, total: int) -> list:
    """Build per-session question order (never mutates cached questions).

    - Hören Teil 1: pair-shuffle (keep [0,1],[2,3],... pairs intact, shuffle pair order).
    - All other standard sections: full random.shuffle.
    - Rule 14: operates on a fresh index list only; cached question dicts untouched.
    """
    order = list(range(total))
    skill_n = (skill or "").lower().strip()
    teil_n = (teil or "").lower().strip()
    if skill_n in ("hören", "hoeren") and teil_n == "teil1":
        pairs = [order[i:i + 2] for i in range(0, total, 2)]
        random.shuffle(pairs)
        return [idx for pair in pairs for idx in pair]
    random.shuffle(order)
    return order


def _resolve_question_order(state_data: dict, total: int) -> list:
    """Return validated question_order from FSM, fallback to identity order.

    Guards stale/truncated state (old sessions without order, or data-tree
    changes altering N): length + range + uniqueness checked.
    """
    order = (state_data or {}).get("question_order")
    if (
        isinstance(order, list)
        and len(order) == total
        and all(isinstance(x, int) and 0 <= x < total for x in order)
        and len(set(order)) == total
    ):
        return order
    return list(range(total))


def _get_ordered_question(questions: list, order: list, current_index: int):
    """Access questions via order mapping without mutating the cached list."""
    if not questions or current_index < 0 or current_index >= len(questions):
        return None
    if current_index >= len(order):
        return questions[current_index]
    mapped = order[current_index]
    if not isinstance(mapped, int) or not 0 <= mapped < len(questions):
        return questions[current_index]
    return questions[mapped]


def generate_progress_bar(current: int, total: int, length: int = 8) -> str:
    """
    توليد شريط تقدم رمزي واضح ونجمي لمساعدة المستخدم على معرفة تقدمه في الاختبار.
    مثال: 📊 السؤال 3 من 10 [🟦🟦🟦⬜⬜⬜⬜⬜] (30%)
    LOW-013: bar rendering delegated to shared utils.render_progress_bar.
    """
    if total <= 0:
        return ""
    percentage = int((current / total) * 100)
    bar = render_progress_bar(percentage, length, filled="🟦", empty="⬜")
    return f"📊 <b>السؤال {current} من {total}</b>\n<code>[{bar}]</code> <b>{percentage}%</b>"

async def safe_edit_message_text_or_send(callback_or_message, text: str, reply_markup=None, parse_mode="HTML"):
    """دالة مساعدة لمعالجة استثناءات التعديل لتفادي تعليق واجهات الأسئلة والـ Quizzes"""
    if isinstance(callback_or_message, types.CallbackQuery):
        try:
            return await callback_or_message.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                await callback_or_message.answer("ℹ️ أنت تقف بالفعل في الصفحة المطلوب الوصول إليها.", show_alert=False)
                return callback_or_message.message
            else:
                return await callback_or_message.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        return await callback_or_message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)

@quiz_router.callback_query(lambda c: c.data == "view_full_text")
async def handle_view_full_text(callback: types.CallbackQuery, state: FSMContext):
    """معالج عرض النص الأساسي كاملاً في نافذة Pop-up أفقية تنبثق للمستخدم"""
    data = await state.get_data()
    skill = data.get('skill', '')
    teil = data.get('teil', '')
    file_name = data.get('file_name', '')

    is_free = await is_free_content(skill, teil, file_name, question_index=0)
    if not is_free and not await can_access_level(callback.from_user.id, "b1"):
        await callback.answer("⚠️ غير مصرح لك بعرض هذا النص. يرجى تفعيل الاشتراك.", show_alert=True)
        return

    title, text_body = '', ''
    if skill and teil and file_name:
        try:
            content = await get_content(skill, teil, file_name)
            parsed = _extract_quiz_content(content, file_name)
            title, text_body = parsed["title"], parsed["text_body"]
        except Exception:
            title, text_body = '', ''

    if not text_body:
        await callback.answer("⚠️ لا يوجد نص أساسي متاح لهذه الأسئلة.", show_alert=True)
        return

    alert_content = f"📄 {title}\n\n{text_body}"
    # الحد الأقصى للنصوص التي تعرضها التلغرام داخل alert هو 2000 حرف تقريباً
    if len(alert_content) > 1900:
        alert_content = alert_content[:1870] + "\n\n... [تم اقتطاع باقي النص لطوله]"

    await callback.answer(alert_content, show_alert=True)

@quiz_router.callback_query(lambda c: c.data and c.data.startswith("read_"))
async def handle_read_text(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    user_id = callback.from_user.id
    parts = callback.data.split("_", 3)
    
    if len(parts) < 4:
        await callback.message.answer("⚠️ نعتذر، حدث خطأ في تحميل بيانات هذا النص. يرجى اختيار نص آخر.", reply_markup=get_cancel_to_main_keyboard())
        return

    skill = os.path.basename(parts[1])
    teil = os.path.basename(parts[2])
    # F-03: callbacks carry a short hash — resolve it server-side.
    file_token = os.path.basename(parts[3])
    resolved = resolve_filename(file_token)
    if resolved is None:
        await callback.answer("⚠️ انتهت صلاحية الزر. يرجى تحديث القائمة.", show_alert=True)
        return
    file_name = os.path.basename(resolved)
    
    is_free = await is_free_content(skill, teil, file_name, question_index=0)

    if not is_free and not await can_access_level(user_id, "b1"):
        await state.clear()
        await state.set_state(AuthState.waiting_for_key)
        text = await get_dynamic_paywall_text()
        kb = get_paywall_keyboard()
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    # State Pointers refactor: content comes from the auto-invalidating cache;
    # FSM keeps only pointers + counters (no question blobs -> no OOM).
    try:
        content = await get_content(skill, teil, file_name)
    except FileNotFoundError:
        await callback.message.answer("⚠️ نعتذر، ملف الأسئلة المطلوب غير متوفر حالياً.", reply_markup=get_cancel_to_main_keyboard())
        return
    except Exception:
        await callback.message.answer("⚠️ تعذر قراءة أسئلة هذا النص، يرجى إبلاغ الدعم الفني.", reply_markup=get_cancel_to_main_keyboard())
        return

    if isinstance(content, dict) and teil.lower() == "teil3" and skill.lower() == "lesen":
        await start_teil3_matching(callback, state, content, skill, teil, file_name)
        return

    parsed = _extract_quiz_content(content, file_name)
    questions_list = parsed["questions"]
    if not questions_list:
        await callback.message.answer("⚠️ تعذر قراءة أسئلة هذا النص، يرجى إبلاغ الدعم الفني.", reply_markup=get_cancel_to_main_keyboard())
        return

    # Pair-shuffle (Hören Teil 1) / normal shuffle (others). Teil3 matching
    # returned early above and keeps its own custom logic. Never shuffle the
    # cached questions list — only the per-session index order (Rule 14).
    total_q = len(questions_list)
    question_order = _build_question_order(skill, teil, total_q)
    await state.clear()
    await state.set_state(QuizState.answering)
    await state.update_data(
        skill=skill,
        teil=teil,
        file_name=file_name,
        current_index=0,
        correct_count=0,
        wrong_count=0,
        question_order=question_order
    )
    await send_quiz_question(callback, state)

@quiz_router.callback_query(lambda c: c.data and c.data.startswith("ans_"))
async def handle_answers(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    state_data = await state.get_data()

    if not state_data or 'file_name' not in state_data:
        await callback.message.answer("⚠️ انتهت الجلسة الحالية أو حدث خطأ، يرجى إعادة اختيار النص.", reply_markup=get_cancel_to_main_keyboard())
        return

    user_id = callback.from_user.id
    skill = state_data.get('skill', '')
    teil = state_data.get('teil', '')
    file_name = state_data.get('file_name', '')
    current_index = state_data.get('current_index', 0)
    correct_count = state_data.get('correct_count', 0)
    wrong_count = state_data.get('wrong_count', 0)

    try:
        content = await get_content(skill, teil, file_name)
    except Exception:
        await callback.message.answer("⚠️ انتهت الجلسة الحالية أو حدث خطأ، يرجى إعادة اختيار النص.", reply_markup=get_cancel_to_main_keyboard())
        return
    parsed = _extract_quiz_content(content, file_name)
    questions = parsed['questions']
    keywords = parsed['keywords']
    # Per-session order (fallback to identity for legacy sessions). Never mutate cache.
    question_order = _resolve_question_order(state_data, len(questions))

    is_free = await is_free_content(skill, teil, file_name, question_index=current_index)

    if not is_free and not await can_access_level(user_id, "b1"):
        # P1 Strike 2: preserve quiz FSM on paywall — do NOT wipe state, just notify and return.
        text = await get_dynamic_paywall_text()
        kb = get_paywall_keyboard()
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    ans_parts = callback.data.split("_")
    if len(ans_parts) < 2 or not ans_parts[1].strip():
        await callback.message.answer("⚠️ خطأ في البيانات.", reply_markup=get_cancel_to_main_keyboard())
        return
    user_choice = ans_parts[1].strip()

    if current_index >= len(questions):
        await callback.message.answer("ℹ️ لقد أتممت حل جميع أسئلة هذا النص سابقاً.", reply_markup=get_cancel_to_main_keyboard())
        return

    current_q = _get_ordered_question(questions, question_order, current_index)
    if current_q is None:
        await callback.message.answer("⚠️ تعذر تحميل السؤال. يرجى المحاولة لاحقاً.", reply_markup=get_cancel_to_main_keyboard())
        return
    correct_answer = str(current_q.get('correct_answer', current_q.get('answer', ''))).strip().upper()
    if correct_answer.isdigit():
        correct_answer = chr(65 + int(correct_answer))  # 0 -> A, 1 -> B, 2 -> C, etc.

    options = current_q.get('options', [])
    if len(correct_answer) > 1:  # It's a full text, not just 'A' or 'B'
        if isinstance(options, list):
            for idx, opt in enumerate(options):
                opt_text = str(opt.get('text', '') if isinstance(opt, dict) else opt).strip().upper()
                if opt_text == correct_answer:
                    correct_answer = chr(65 + idx)  # Convert index to A, B, C...
                    break
        elif isinstance(options, dict):
            for k, v in options.items():
                if str(v).strip().upper() == correct_answer:
                    correct_answer = str(k).upper()
                    break

    norm_map = {
        "J": "JA", "JA": "JA", "YES": "JA",
        "N": "NEIN", "NEIN": "NEIN", "NO": "NEIN",
        "R": "RICHTIG", "RICHTIG": "RICHTIG", "TRUE": "RICHTIG",
        "F": "FALSCH", "FALSCH": "FALSCH", "FALSE": "FALSCH",
        "A": "A", "B": "B", "C": "C", "D": "D"
    }

    if user_choice.upper() == "A":
        if correct_answer.upper() in ["R", "RICHTIG", "TRUE"]:
            user_norm = "RICHTIG"
        elif correct_answer.upper() in ["J", "JA", "YES"]:
            user_norm = "JA"
        else:
            user_norm = "A"
    elif user_choice.upper() == "B":
        if correct_answer.upper() in ["F", "FALSCH", "FALSE"]:
            user_norm = "FALSCH"
        elif correct_answer.upper() in ["N", "NEIN", "NO"]:
            user_norm = "NEIN"
        else:
            user_norm = "B"
    else:
        user_norm = norm_map.get(user_choice.upper(), user_choice.upper())

    correct_norm = norm_map.get(correct_answer.upper(), correct_answer.upper())
    is_correct = (user_norm == correct_norm)

    await record_answer_stat(callback.from_user.id, is_correct)

    if is_correct:
        correct_count += 1
        feedback = "✨ <b>إجابة صحيحة! أحسنت.</b> ✅"
    else:
        wrong_count += 1
        correct_str = correct_answer.upper()
        full_text_ans = ""
        
        options = current_q.get('options', [])
        if isinstance(options, list):
            for idx_opt, opt in enumerate(options):
                if isinstance(opt, dict):
                    opt_k = str(opt.get('key', '')).strip().upper()
                    if opt_k == correct_str:
                        # F-04: escape untrusted option text before HTML render.
                        full_text_ans = f"<b>{html.escape(opt_k)})</b> {html.escape(str(opt.get('text', '')))}"
                        break
                else:
                    opt_letter = chr(97 + idx_opt).upper()
                    if opt_letter == correct_str:
                        full_text_ans = f"<b>{html.escape(opt_letter)})</b> {html.escape(str(opt))}"
                        break
        elif isinstance(options, dict):
            for opt_k, opt_v in options.items():
                if str(opt_k).strip().upper() == correct_str:
                    full_text_ans = f"<b>{html.escape(str(opt_k).upper())})</b> {html.escape(str(opt_v))}"
                    break

        if not full_text_ans:
            if correct_str in ["J", "JA", "R", "RICHTIG"]:
                full_text_ans = "Richtig / Ja"
            elif correct_str in ["N", "NEIN", "F", "FALSCH"]:
                full_text_ans = "Falsch / Nein"
            else:
                full_text_ans = html.escape(correct_str)

        feedback = f"⚠️ <b>إجابة خاطئة!</b>\nالإجابة الصحيحة هي: {full_text_ans} ❌"

    next_index = current_index + 1
    await state.update_data(current_index=next_index, correct_count=correct_count, wrong_count=wrong_count)

    if next_index >= len(questions):
        user_id = callback.from_user.id
        skill = os.path.basename(skill)
        teil = os.path.basename(teil)
        file_name = os.path.basename(file_name)

        text_id = f"{skill}_{teil}_{file_name}"
        await mark_text_completed(user_id, text_id)

        total_answered = correct_count + wrong_count
        score_percentage = round((correct_count * 100) / total_answered) if total_answered > 0 else 0

        finish_text = (
            f"📖 <b>نتيجة السؤال الأخير:</b>\n{feedback}\n\n"
            f"----------------------------------------\n"
            f"🎉 <b>أحسنت! أتممت جميع أسئلة هذا النص.</b>\n"
            f"تم وضع علامة (✅) على النص.\n\n"
            f"📊 <b>تقرير الأداء النهائي للجلسة:</b>\n"
            f"✅ الإجابات الصحيحة: <b>{correct_count}</b>\n"
            f"❌ الإجابات الخاطئة: <b>{wrong_count}</b>\n"
            f"📈 نسبة النجاح: <b>{score_percentage}%</b>"
        )
        
        if keywords:
            # F-04: escape untrusted keywords before HTML render.
            finish_text += f"\n\n🔑 <b>الكلمات المفتاحية:</b> {html.escape(str(keywords))}"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 العودة للقائمة", callback_data=f"b1_parts_{skill}_{teil}")],
            [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="main_menu")]
        ])
        await state.clear()
        await safe_edit_message_text_or_send(callback, finish_text, reply_markup=kb, parse_mode="HTML")
    else:
        skill = state_data.get('skill', '')
        teil = state_data.get('teil', '')
        file_name = state_data.get('file_name', '')
        user_id = callback.from_user.id

        if not await can_access_level(user_id, "b1") and not (await is_free_content(skill, teil, file_name, question_index=next_index)):
            # P1 Strike 2: preserve quiz FSM on paywall — do NOT wipe state, just notify and return.
            text = await get_dynamic_paywall_text()
            kb = get_paywall_keyboard()
            await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
            return

        await send_quiz_question(callback, state, show_feedback=feedback)

@quiz_router.callback_query(lambda c: c.data == "skip_question")
async def handle_skip_question(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("تم تجاوز السؤال ⏭️")
    state_data = await state.get_data()

    if not state_data or 'file_name' not in state_data:
        await callback.message.answer("⚠️ انتهت الجلسة الحالية أو حدث خطأ، يرجى إعادة اختيار النص.", reply_markup=get_cancel_to_main_keyboard())
        return

    user_id = callback.from_user.id
    skill = state_data.get('skill', '')
    teil = state_data.get('teil', '')
    file_name = state_data.get('file_name', '')
    current_index = state_data.get('current_index', 0)
    correct_count = state_data.get('correct_count', 0)
    wrong_count = state_data.get('wrong_count', 0)

    try:
        content = await get_content(skill, teil, file_name)
    except Exception:
        await callback.message.answer("⚠️ انتهت الجلسة الحالية أو حدث خطأ، يرجى إعادة اختيار النص.", reply_markup=get_cancel_to_main_keyboard())
        return
    parsed = _extract_quiz_content(content, file_name)
    questions = parsed['questions']
    keywords = parsed['keywords']
    # Retrieve order for consistency (skip advances positional index; mapping
    # is applied on render via send_quiz_question). Fallback keeps legacy sessions alive.
    _resolve_question_order(state_data, len(questions))

    if current_index >= len(questions):
        await callback.message.answer("ℹ️ لقد أتممت حل أو تجاوز جميع أسئلة هذا النص سابقاً.", reply_markup=get_cancel_to_main_keyboard())
        return

    is_free = await is_free_content(skill, teil, file_name, question_index=current_index)

    if not is_free and not await can_access_level(user_id, "b1"):
        # P1 Strike 2: preserve quiz FSM on paywall — do NOT wipe state, just notify and return.
        text = await get_dynamic_paywall_text()
        kb = get_paywall_keyboard()
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    next_index = current_index + 1
    await state.update_data(current_index=next_index)

    if next_index >= len(questions):
        user_id = callback.from_user.id
        skill_name = os.path.basename(skill)
        teil_name = os.path.basename(teil)
        file_name_val = os.path.basename(file_name)

        text_id = f"{skill_name}_{teil_name}_{file_name_val}"
        await mark_text_completed(user_id, text_id)

        total_answered = correct_count + wrong_count
        score_percentage = round((correct_count * 100) / total_answered) if total_answered > 0 else 0
        
        finish_text = (
            f"🎉 <b>أحسنت! أتممت تصفح جميع أسئلة هذا النص.</b>\nتم وضع علامة (✅) على النص.\n\n"
            f"📊 <b>تقرير الأداء النهائي للجلسة:</b>\n"
            f"✅ الإجابات الصحيحة: <b>{correct_count}</b>\n"
            f"❌ الإجابات الخاطئة: <b>{wrong_count}</b>\n"
            f"📈 نسبة النجاح: <b>{score_percentage}%</b>"
        )
        if keywords:
            # F-04: escape untrusted keywords before HTML render.
            finish_text += f"\n\n🔑 <b>الكلمات المفتاحية:</b> {html.escape(str(keywords))}"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 العودة للقائمة", callback_data=f"b1_parts_{skill_name}_{teil_name}")],
            [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="main_menu")]
        ])
        await state.clear()
        await safe_edit_message_text_or_send(callback, finish_text, reply_markup=kb, parse_mode="HTML")
    else:
        if not await can_access_level(user_id, "b1") and not (await is_free_content(skill, teil, file_name, question_index=next_index)):
            # P1 Strike 2: preserve quiz FSM on paywall — do NOT wipe state, just notify and return.
            text = await get_dynamic_paywall_text()
            kb = get_paywall_keyboard()
            await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
            return

        await send_quiz_question(callback, state)

@quiz_router.callback_query(lambda c: c.data == "prev_question")
async def handle_prev_question(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("العودة للسؤال السابق ⬅️")
    state_data = await state.get_data()

    if not state_data or 'file_name' not in state_data:
        await callback.message.answer("⚠️ انتهت الجلسة الحالية أو حدث خطأ، يرجى إعادة اختيار النص.", reply_markup=get_cancel_to_main_keyboard())
        return

    user_id = callback.from_user.id
    skill = state_data.get('skill', '')
    teil = state_data.get('teil', '')
    file_name = state_data.get('file_name', '')
    current_index = state_data.get('current_index', 0)
    # Retrieve order to keep session consistent (render maps via order).
    # Fallback preserves legacy sessions without question_order.
    try:
        content_probe = await get_content(skill, teil, file_name)
        _resolve_question_order(state_data, len(_extract_quiz_content(content_probe, file_name)['questions']))
    except Exception:
        pass

    if current_index <= 0:
        await callback.answer("ℹ️ أنت بالفعل عند السؤال الأول.", show_alert=True)
        return

    prev_index = current_index - 1
    # HIGH-003: explicit paywall gate on the TARGET index before moving.
    # Do NOT update state unless the target question is free or the user has access.
    prev_is_free = await is_free_content(skill, teil, file_name, question_index=prev_index)
    if not prev_is_free and not await can_access_level(user_id, "b1"):
        text = await get_dynamic_paywall_text()
        kb = get_paywall_keyboard()
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    await state.update_data(current_index=prev_index)
    await send_quiz_question(callback, state)

@quiz_router.callback_query(Teil3State.matching, lambda c: c.data and (c.data.startswith("t3_q_") or c.data.startswith("t3_a_")))
async def handle_teil3_events(callback: types.CallbackQuery, state: FSMContext):
    await handle_teil3_click(callback, state)

# --- الدوال المساعدة للاختبارات ---
async def start_teil3_matching(callback: types.CallbackQuery, state: FSMContext, content: dict, skill: str, teil: str, file_name: str):
    raw_pairs = content.get("pairs", content.get("questions", []))
    questions, answers, correct_matches = [], [], {}
    
    for idx, item in enumerate(raw_pairs):
        q_id, a_id = f"Q{idx}", f"A{idx}"
        q_text = item.get("question", item.get("statement", f"موقف {idx+1}"))
        a_text = item.get("answer", item.get("option", f"إجابة {idx+1}"))
        questions.append({"id": q_id, "text": q_text})
        answers.append({"id": a_id, "text": a_text})
        correct_matches[q_id] = a_id

    shuffled_answers = answers.copy()
    random.shuffle(shuffled_answers)

    remaining_q_ids = [q["id"] for q in questions]
    remaining_a_ids = [a["id"] for a in shuffled_answers]

    await state.set_state(Teil3State.matching)
    await state.update_data(
        title=content.get('title', 'Teil 3 Matching'),
        questions=questions,
        answers=shuffled_answers,
        correct_matches=correct_matches,
        remaining_q_ids=remaining_q_ids,
        remaining_a_ids=remaining_a_ids,
        completed_matches={},
        correct_count=0,
        wrong_count=0,
        selected_q=None,
        selected_a=None,
        error_pair=None,
        keywords=content.get("keywords", {}),
        raw_pairs=raw_pairs,
        skill=os.path.basename(skill),
        teil=os.path.basename(teil),
        file_name=os.path.basename(file_name)
    )

    sent_msg = await render_teil3_view(callback, state)
    if sent_msg and hasattr(sent_msg, 'message_id'):
        await state.update_data(main_message_id=sent_msg.message_id)

async def render_teil3_view(callback_or_message, state: FSMContext):
    data = await state.get_data()
    questions, answers = data["questions"], data["answers"]
    remaining_q_ids, remaining_a_ids = data["remaining_q_ids"], data["remaining_a_ids"]
    selected_q, selected_a = data.get("selected_q"), data.get("selected_a")

    total_pairs = len(questions)
    completed_pairs = total_pairs - len(remaining_q_ids)
    progress_text = generate_progress_bar(completed_pairs, total_pairs)

    msg_text = f"🧩 <b>{html.escape(str(data.get('title', '')))}</b>\n{progress_text}\n\n🔹 <b>الأسئلة / المواقف المتبقية:</b>\n"
    q_dict = {q["id"]: q["text"] for q in questions}
    for q_id in remaining_q_ids:
        msg_text += f"<b>Q{int(q_id.replace('Q', '')) + 1}:</b> {html.escape(str(q_dict[q_id]))}\n"

    msg_text += "\n🔸 <b>الأجوبة / الخيارات المتبقية:</b>\n"
    a_dict = {a["id"]: a["text"] for a in answers}
    for a_id in remaining_a_ids:
        orig_idx = next(i for i, a in enumerate(answers) if a["id"] == a_id) + 1
        ans_text = a_dict[a_id]
        display_text = "keine" if ans_text.strip().lower() == "keine" else html.escape(str(ans_text))
        msg_text += f"<b>A{orig_idx}:</b> {display_text}\n"

    msg_text += "\n👇 <i>اضغط على سؤال ثم على إجابته المناسبة للوصل بينهما:</i>"

    buttons, q_row = [], []
    for q_id in remaining_q_ids:
        q_num = int(q_id.replace("Q", "")) + 1
        btn_label = f"🔘 Q{q_num}" if selected_q == q_id else f"Q{q_num}"
        q_row.append(InlineKeyboardButton(text=btn_label, callback_data=f"t3_q_{q_id}"))
    if q_row: buttons.append(q_row)

    a_row = []
    for a_id in remaining_a_ids:
        orig_idx = next(i for i, a in enumerate(answers) if a["id"] == a_id) + 1
        btn_label = f"🔘 A{orig_idx}" if selected_a == a_id else f"A{orig_idx}"
        a_row.append(InlineKeyboardButton(text=btn_label, callback_data=f"t3_a_{a_id}"))
    if a_row: buttons.append(a_row)

    buttons.append([
        InlineKeyboardButton(text="🔙 إنهاء الاختبار", callback_data=f"b1_parts_{data['skill']}_{data['teil']}"),
        InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="main_menu")
    ])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    return await safe_edit_message_text_or_send(callback_or_message, msg_text, reply_markup=markup, parse_mode="HTML")

async def handle_teil3_click(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    skill = data.get('skill', '')
    teil = data.get('teil', '')
    file_name = data.get('file_name', '')

    is_free = await is_free_content(skill, teil, file_name, question_index=0)
    if not is_free and not await can_access_level(callback.from_user.id, "b1"):
        text = await get_dynamic_paywall_text()
        kb = get_paywall_keyboard()
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    click_data = callback.data
    selected_q, selected_a = data.get("selected_q"), data.get("selected_a")

    if click_data.startswith("t3_q_"):
        q_id = click_data.replace("t3_q_", "")
        if q_id not in data.get("remaining_q_ids", []):
            return await callback.answer()
        selected_q = q_id if selected_q != q_id else None
        await state.update_data(selected_q=selected_q)
    elif click_data.startswith("t3_a_"):
        a_id = click_data.replace("t3_a_", "")
        if a_id not in data.get("remaining_a_ids", []):
            return await callback.answer()
        selected_a = a_id if selected_a != a_id else None
        await state.update_data(selected_a=selected_a)

    if selected_q and selected_a:
        is_correct = (data["correct_matches"].get(selected_q) == selected_a)
        await record_answer_stat(callback.from_user.id, is_correct)
        
        correct_count = data.get('correct_count', 0)
        wrong_count = data.get('wrong_count', 0)

        if is_correct:
            await callback.answer("✨ إجابة صحيحة! تم الربط بنجاح. ✅", show_alert=False)
            correct_count += 1
            rem_q = [q for q in data["remaining_q_ids"] if q != selected_q]
            rem_a = [a for a in data["remaining_a_ids"] if a != selected_a]
            completed_matches = data.get("completed_matches", {})
            completed_matches[selected_q] = selected_a

            await state.update_data(
                remaining_q_ids=rem_q,
                remaining_a_ids=rem_a,
                completed_matches=completed_matches,
                selected_q=None,
                selected_a=None,
                correct_count=correct_count,
                wrong_count=wrong_count
            )
            if not rem_q:
                await finish_teil3_matching(callback, state)
            else:
                await render_teil3_view(callback, state)
        else:
            await callback.answer("❌ التوصيل غير صحيح! حاول مرة أخرى.", show_alert=True)
            wrong_count += 1
            await state.update_data(
                selected_q=None,
                selected_a=None,
                correct_count=correct_count,
                wrong_count=wrong_count
            )
            await render_teil3_view(callback, state)
    else:
        await callback.answer()
        await render_teil3_view(callback, state)

async def finish_teil3_matching(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    skill = os.path.basename(data['skill'])
    teil = os.path.basename(data['teil'])
    file_name = os.path.basename(data['file_name'])
    
    await mark_text_completed(user_id, f"{skill}_{teil}_{file_name}")

    correct_count = data.get('correct_count', 0)
    wrong_count = data.get('wrong_count', 0)
    
    total_answered = correct_count + wrong_count
    score_percentage = round((correct_count * 100) / total_answered) if total_answered > 0 else 0

    finish_text = (
        "🎉 <b>ممتاز جداً! لقد أكملت وصل جميع الأسئلة والأجوبة بنجاح.</b>\n"
        "----------------------------------------\n"
        "📊 <b>تقرير الأداء النهائي للجلسة:</b>\n"
        f"✅ المحاولات الصحيحة: <b>{correct_count}</b>\n"
        f"❌ المحاولات الخاطئة: <b>{wrong_count}</b>\n"
        f"📈 نسبة النجاح: <b>{score_percentage}%</b>\n"
        "----------------------------------------\n\n"
        "🔑 <b>الكلمات المفتاحية والأسئلة كاملة:</b>\n\n"
    )
    for idx, item in enumerate(data.get('raw_pairs', []), 1):
        q_text = item.get("question", item.get("statement", f"موقف {idx}"))
        a_text = item.get("answer", item.get("option", f"إجابة {idx}"))
        kw = data.get('keywords', {}).get(str(idx), item.get("keywords", "غير محددة"))
        # F-04: escape untrusted JSON content before HTML render.
        finish_text += f"❓ <b>السؤال {idx}:</b> {html.escape(str(q_text))}\n✅ <b>الجواب:</b> {html.escape(str(a_text))}\n💡 <b>الكلمات المفتاحية:</b> <code>{html.escape(str(kw))}</code>\n----------------------------------------\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 العودة للتحضير", callback_data=f"b1_parts_{skill}_{teil}")],
        [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="main_menu")]
    ])
    await state.clear()
    # F16: Telegram 4096-char limit — chunk long finish text.
    MAX_CHUNK = 4000
    chunks = [finish_text[i:i + MAX_CHUNK] for i in range(0, len(finish_text), MAX_CHUNK)] or [finish_text]
    for idx, chunk in enumerate(chunks):
        if idx == len(chunks) - 1:
            await callback.message.answer(chunk, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.answer(chunk, parse_mode="HTML")

async def send_quiz_question(callback_or_message, state: FSMContext, show_feedback=None):
    data = await state.get_data()
    skill = data.get('skill', '')
    teil = data.get('teil', '')
    file_name = data.get('file_name', '')
    current_index = data.get('current_index', 0)

    if not skill or not teil or not file_name:
        await safe_edit_message_text_or_send(callback_or_message, "⚠️ تعذر تحميل السؤال. يرجى المحاولة لاحقاً.")
        return
    try:
        content = await get_content(skill, teil, file_name)
    except Exception:
        await safe_edit_message_text_or_send(callback_or_message, "⚠️ تعذر تحميل السؤال. يرجى المحاولة لاحقاً.")
        return
    parsed = _extract_quiz_content(content, file_name)
    questions = parsed['questions']
    title = parsed['title']
    summary = parsed['summary']
    fixed_question = parsed['fixed_question']
    text_body = parsed['text_body']

    if not questions or current_index >= len(questions):
        await safe_edit_message_text_or_send(callback_or_message, "⚠️ تعذر تحميل السؤال. يرجى المحاولة لاحقاً.")
        return

    # Order mapping: positional current_index -> original question idx.
    # Falls back to identity for legacy sessions. Never mutates cache.
    question_order = _resolve_question_order(data, len(questions))
    q = _get_ordered_question(questions, question_order, current_index)
    if q is None:
        await safe_edit_message_text_or_send(callback_or_message, "⚠️ تعذر تحميل السؤال. يرجى المحاولة لاحقاً.")
        return
    total = len(questions)

    progress_bar = generate_progress_bar(current_index + 1, total)
    q_text_content = q.get('question') or q.get('text') or ''

    is_teil4 = (teil.lower() == "teil4" and skill.lower() == "lesen")
    is_teil5 = (teil.lower() == "teil5" and skill.lower() == "lesen")

    q_msg_text = ""
    if show_feedback:
        q_msg_text += f"{show_feedback}\n\n"

    q_msg_text += f"{progress_bar}\n\n"

    if is_teil4:
        if fixed_question:
            # F-04: escape untrusted JSON content before HTML render.
            q_msg_text += f"📌 <b>السؤال العام:</b>\n<code>{html.escape(str(fixed_question))}</code>\n\n"
        q_msg_text += f"📄 <b>النص / التعليق ({current_index + 1}/{total}):</b>\n{html.escape(str(q_text_content))}\n\n"
        kb = get_quiz_navigation_keyboard(skill, teil, current_index, total, teil4=True)
    elif is_teil5:
        q_msg_text += f"❓ <b>سؤال القواعد ({current_index + 1}/{total}):</b>\n<code>{html.escape(str(q_text_content))}</code>\n\n"
        options = q.get('options')
        if options:
            if isinstance(options, list):
                for idx_opt, opt_item in enumerate(options):
                    if isinstance(opt_item, dict):
                        opt_key, opt_text = opt_item.get('key', chr(97 + idx_opt)), opt_item.get('text', '')
                        q_msg_text += f"<b>{html.escape(str(opt_key).upper())})</b> {html.escape(str(opt_text))}\n"
                    else:
                        opt_letter = chr(97 + idx_opt).upper()
                        q_msg_text += f"<b>{html.escape(opt_letter)})</b> {html.escape(str(opt_item))}\n"
            elif isinstance(options, dict):
                for opt_key, opt_text in options.items():
                    q_msg_text += f"<b>{html.escape(str(opt_key).upper())})</b> {html.escape(str(opt_text))}\n"
        kb = get_quiz_navigation_keyboard(skill, teil, current_index, total, options=options)
    else:
        q_msg_text += f"❓ <b>السؤال:</b>\n<code>{html.escape(str(q_text_content))}</code>\n\n"
        options = q.get('options')
        is_tf_options = False
        if isinstance(options, dict):
            if any(str(v).strip().lower() in ["richtig", "falsch", "true", "false"] for v in options.values()):
                is_tf_options = True

        if options and not is_tf_options:
            if isinstance(options, list):
                for idx_opt, opt_item in enumerate(options):
                    if isinstance(opt_item, dict):
                        opt_key, opt_text = opt_item.get('key', chr(97 + idx_opt)), opt_item.get('text', '')
                        q_msg_text += f"<b>{html.escape(str(opt_key).upper())})</b> {html.escape(str(opt_text))}\n"
                    else:
                        opt_letter = chr(97 + idx_opt).upper()
                        q_msg_text += f"<b>{html.escape(opt_letter)})</b> {html.escape(str(opt_item))}\n"
            elif isinstance(options, dict):
                for opt_key, opt_text in options.items():
                    q_msg_text += f"<b>{html.escape(str(opt_key).upper())})</b> {html.escape(str(opt_text))}\n"

            kb = get_quiz_navigation_keyboard(skill, teil, current_index, total, options=options)
        else:
            kb = get_quiz_navigation_keyboard(skill, teil, current_index, total, options=None)

    if text_body:
        btn_view_text = InlineKeyboardButton(text="📄 عرض النص الأساسي", callback_data="view_full_text")
        if hasattr(kb, 'inline_keyboard'):
            kb.inline_keyboard.insert(0, [btn_view_text])

    # F-04: escape untrusted content titles/summaries before HTML render.
    final_text = f"📖 <b>{html.escape(str(title))}</b>\n"
    if summary:
        final_text += f"<i>{html.escape(str(summary))}</i>\n"
    final_text += f"\n{q_msg_text}"

    # F-08: Telegram 4096-char cap — chunk long questions like finish_teil3.
    MAX_CHUNK = 4000
    if len(final_text) > MAX_CHUNK:
        chunks = [final_text[i:i + MAX_CHUNK] for i in range(0, len(final_text), MAX_CHUNK)]
        for chunk in chunks[:-1]:
            await callback_or_message.answer(chunk, parse_mode="HTML")
        await safe_edit_message_text_or_send(callback_or_message, chunks[-1], reply_markup=kb, parse_mode="HTML")
        return

    await safe_edit_message_text_or_send(callback_or_message, final_text, reply_markup=kb, parse_mode="HTML")
const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand();
const params = new URLSearchParams(location.search);
const isAdmin = params.get('admin') === '1';
const initData = tg?.initData || '';
const adminSecret = localStorage.getItem('ss25-admin-secret') || '';
const apiHeaders = { 'Content-Type': 'application/json', ...(initData ? { 'X-Telegram-Init-Data': initData } : {}), ...(isAdmin && adminSecret ? { 'X-SS25-Admin': adminSecret } : {}) };
const q = (s) => document.querySelector(s);
async function api(path, options = {}) {
  if (location.hostname.endsWith('.github.io')) {
    throw new Error('Интерфейс опубликован. Сервер приложения ещё не подключён: обучение, проверка заданий и заказы пока недоступны.');
  }
  const response = await fetch(path, { ...options, headers: { ...apiHeaders, ...(options.headers || {}) } });
  if (!response.headers.get('Content-Type')?.includes('application/json')) throw new Error('Сервер приложения недоступен.');
  const data = await response.json();
  if (!response.ok) throw new Error(response.status === 401 ? 'Откройте приложение через меню Telegram-бота для входа.' : data.error || 'Ошибка сервера');
  return data;
}
function notice(text, error = false) { const box = q('#miniNotice'); box.hidden = false; box.textContent = text; box.className = `mini-notice ${error ? 'error' : ''}`; }
function renderDays(data) { q('#progressLabel').textContent = `${data.completed}/14`; q('#progressBar').style.width = `${data.completed / 14 * 100}%`; q('#progressState').textContent = data.completed === 14 ? 'Экзамен завершён' : `Открыт день ${data.unlocked_day}`; q('#days').innerHTML = data.days.map((day) => { const row = data.progress[day.day - 1]; const unlocked = day.day <= data.unlocked_day; return `<button class="mini-day ${unlocked ? 'unlocked' : ''} ${row?.status === 'accepted' ? 'done' : ''}" data-day="${day.day}" ${unlocked ? '' : 'disabled'}><b>${String(day.day).padStart(2,'0')}</b><span>${day.title}</span><small>${row?.status === 'pending' ? 'На проверке' : row?.status === 'revision' ? 'Нужна доработка' : row?.status === 'accepted' ? 'Принято' : unlocked ? 'Открыто' : 'Закрыто'}</small></button>`; }).join(''); document.querySelectorAll('[data-day]').forEach((button) => button.onclick = () => selectDay(data, Number(button.dataset.day))); selectDay(data, data.unlocked_day); }
function selectDay(data, day) { const task = data.days[day - 1]; const row = data.progress[day - 1] || {}; q('#taskDay').textContent = `DAY ${String(day).padStart(2,'0')}`; q('#taskTitle').textContent = task.title; q('#taskText').textContent = task.task; q('#answer').value = row.answer || ''; q('#answer').disabled = row.status === 'pending' || row.status === 'accepted'; q('#submitTask').disabled = q('#answer').disabled; q('#submitState').textContent = row.status === 'pending' ? 'Ответ отправлен. Ожидает проверки менеджером.' : row.status === 'accepted' ? 'Задание принято. Следующий блок открыт.' : row.status === 'revision' ? 'Нужна доработка. Исправьте ответ и отправьте снова.' : ''; q('#submitTask').onclick = async () => { try { await api('/api/mini/training/submit', { method: 'POST', body: JSON.stringify({ day, answer: q('#answer').value }) }); notice('Ответ отправлен менеджеру на проверку.'); await loadTraining(); } catch (error) { notice(error.message, true); } }; }
async function loadTraining() { try { const data = await api('/api/mini/training'); q('#miniTitle').textContent = `Обучение, ${data.user.name || 'менеджер'}`; q('#miniRole').textContent = 'MANAGER'; q('#trainingView').hidden = false; renderDays(data); } catch (error) { notice(error.message, true); } }
async function loadClient() { try { q('#miniTitle').textContent = 'Заказ SS25'; q('#miniRole').textContent = 'CLIENT'; q('#clientView').hidden = false; await loadOrders(); } catch (error) { notice(error.message, true); } }
async function loadOrders() { const rows = await api('/api/mini/client/orders'); q('#orders').innerHTML = rows.length ? rows.map((row) => `<div class="mini-order"><b>${row.id}</b><span>${row.model}</span><small>${row.status} · ${Number(row.total_rub).toLocaleString('ru-RU')} ₽${row.tracking_number ? ` · ${row.tracking_number}` : ''}</small></div>`).join('') : '<p class="muted">Заказов пока нет.</p>'; }
q('#miniOrderForm').onsubmit = async (event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.target)); try { const calc = await api('/api/calculate', { method: 'POST', body: JSON.stringify(data) }); const order = await api('/api/mini/client/orders', { method: 'POST', body: JSON.stringify(data) }); q('#orderResult').innerHTML = `<div class="receipt"><b>${order.id}</b><p>К оплате: <strong>${Number(calc.total_rub).toLocaleString('ru-RU')} ₽</strong></p><small>Заказ создан и ожидает подтверждения администратора.</small></div>`; event.target.reset(); await loadOrders(); } catch (error) { notice(error.message, true); } };
async function loadReviews() { try { const rows = await api('/api/mini/admin/reviews'); q('#reviews').innerHTML = rows.length ? rows.map((row) => `<div class="mini-review"><b>День ${row.day + 1} · ${row.name || row.telegram_id}</b><small>${row.status === 'new' ? 'Новая проверка' : row.status === 'accepted' ? 'Принято' : 'На доработке'}</small><p>${row.answer || 'Ответ отсутствует'}</p><div class="review-actions"><button data-review="${row.id}" data-status="accepted" class="secondary-btn">Принять</button><button data-review="${row.id}" data-status="revision" class="text-btn">На доработку</button></div></div>`).join('') : '<p class="muted">Новых заданий нет.</p>'; document.querySelectorAll('[data-review]').forEach((button) => button.onclick = async () => { await api(`/api/reviews/${button.dataset.review}/check`, { method: 'POST', body: JSON.stringify({ status: button.dataset.status }) }); await loadReviews(); }); } catch (error) { notice(error.message, true); } }
if (isAdmin) { q('#adminView').hidden = false; q('#miniTitle').textContent = 'Проверка обучения'; q('#miniRole').textContent = 'ADMIN'; q('#reloadReviews').onclick = loadReviews; loadReviews(); } else if (params.get('client') === '1') { loadClient(); } else { loadTraining(); }


if (location.hostname.endsWith('.github.io')) {
  q('#miniTitle').textContent = isAdmin ? 'Проверка обучения' : params.get('client') === '1' ? 'Заказы SS25' : 'Обучение SS25';
  q('#miniRole').textContent = 'НЕТ ПОДКЛЮЧЕНИЯ';
  document.querySelectorAll('form input, form select, form button, #submitTask').forEach(element => element.disabled = true);
}

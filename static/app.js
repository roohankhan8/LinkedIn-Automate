(function () {
    const tabs = document.querySelectorAll('.nav-btn');
    const panels = document.querySelectorAll('.tab-panel');
    const btnLogin = document.getElementById('btn-login');
    const btnLoginDone = document.getElementById('btn-login-done');
    const btnSearch = document.getElementById('btn-search');
    const statusCard = document.getElementById('status-card');
    const statusText = document.getElementById('status-text');
    const spinner = document.getElementById('spinner');
    const logView = document.getElementById('log-view');
    const btnCopyLogs = document.getElementById('btn-copy-logs');
    const btnOllamaLogs = document.getElementById('btn-ollama-logs');
    const btnCopyOllamaLogs = document.getElementById('btn-copy-ollama-logs');
    const ollamaLogView = document.getElementById('ollama-log-view');
    const errorBanner = document.getElementById('error-banner');
    const errorText = document.getElementById('error-text');
    const editors = {
        config: document.getElementById('config-editor'),
        env: document.getElementById('env-editor'),
        resume: document.getElementById('resume-editor'),
        qa: document.getElementById('qa-editor'),
    };

    function showTab(tab) {
        panels.forEach(p => p.classList.add('hidden'));
        tabs.forEach(t => t.classList.remove('active'));
        document.getElementById(tab).classList.remove('hidden');
        document.querySelector(`[data-tab="${tab}"]`).classList.add('active');
    }

    tabs.forEach(btn => {
        btn.addEventListener('click', () => showTab(btn.dataset.tab));
    });

    function updateLogView(logs) {
        if (logs.length === 0) return;
        logView.textContent = logs.join('\n');
        logView.scrollTop = logView.scrollHeight;
    }

    async function loadOllamaLogs() {
        ollamaLogView.textContent = 'Loading...';
        try {
            const res = await fetch('/api/ollama/logs');
            const data = await res.json();
            if (res.ok) {
                ollamaLogView.textContent = data.logs || 'No logs.';
            } else {
                ollamaLogView.textContent = 'Error: ' + (data.error || 'Failed to load Ollama logs.');
            }
        } catch (e) {
            ollamaLogView.textContent = 'Error: ' + e.message;
        }
    }

    async function copyLog(view, button) {
        const text = view.textContent;
        try {
            await navigator.clipboard.writeText(text);
        } catch (e) {
            const selection = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(view);
            selection.removeAllRanges();
            selection.addRange(range);
            document.execCommand('copy');
            selection.removeAllRanges();
        }
        const label = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => { button.textContent = label; }, 1200);
    }

    async function getJSON(url) {
        const res = await fetch(url);
        return res.json();
    }

    async function postJSON(url, data) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        return res.json();
    }

    async function loadConfig() {
        const data = await getJSON('/api/config');
        editors.config.value = JSON.stringify(data, null, 2);
    }

    async function loadEnv() {
        const data = await getJSON('/api/env');
        editors.env.value = data.value;
    }

    async function loadResume() {
        const data = await getJSON('/api/resume_profile');
        editors.resume.value = JSON.stringify(data, null, 2);
    }

    async function loadQA() {
        const data = await getJSON('/api/qa_cache');
        editors.qa.value = JSON.stringify(data, null, 2);
    }

    async function saveConfig() {
        try {
            const data = JSON.parse(editors.config.value);
            await postJSON('/api/config', data);
            alert('Config saved.');
        } catch (e) {
            alert('Invalid JSON: ' + e.message);
        }
    }

    async function saveEnv() {
        await postJSON('/api/env', { value: editors.env.value });
        alert('Env saved.');
    }

    async function saveResume() {
        try {
            const data = JSON.parse(editors.resume.value);
            await postJSON('/api/resume_profile', data);
            alert('Resume profile saved.');
        } catch (e) {
            alert('Invalid JSON: ' + e.message);
        }
    }

    async function saveQA() {
        try {
            const data = JSON.parse(editors.qa.value);
            await postJSON('/api/qa_cache', data);
            alert('QA cache saved.');
        } catch (e) {
            alert('Invalid JSON: ' + e.message);
        }
    }

    async function startLogin() {
        const data = await postJSON('/api/login', {});
        if (!data.started) {
            alert('Another task is already running.');
        }
    }

    async function startSearch() {
        const data = await postJSON('/api/search', {});
        if (!data.started) {
            alert('Another task is already running.');
        }
    }

    async function checkLoginStatus() {
        const data = await getJSON('/api/login/status');
        btnSearch.disabled = !data.logged_in;
        if (!data.logged_in) {
            btnSearch.title = 'Run login first';
        } else {
            btnSearch.title = 'Start search and apply';
        }
    }

    async function confirmLogin() {
        await postJSON('/api/login/confirm', {});

        for (let i = 0; i < 20; i++) {
            await new Promise(r => setTimeout(r, 1000));
            const data = await getJSON('/api/login/status');
            if (data.logged_in) {
                alert('Login confirmed. Search is now enabled.');
                await checkLoginStatus();
                return;
            }
        }

        alert('Login not detected. Complete the browser login first.');
        await checkLoginStatus();
    }

    async function pollStatus() {
        const data = await getJSON('/api/status');
        if (data.running) {
            statusCard.classList.remove('hidden');
            statusText.textContent = `Running: ${data.task}`;
            spinner.classList.remove('hidden');
            errorBanner.classList.add('hidden');
        } else {
            statusCard.classList.add('hidden');
            statusText.textContent = 'Idle';
            spinner.classList.add('hidden');
            if (data.error) {
                errorBanner.classList.remove('hidden');
                errorText.textContent = `Task "${data.task || 'last run'}" failed. Check the Logs tab for details.`;
            } else {
                errorBanner.classList.add('hidden');
            }
        }
        updateLogView(data.logs);
        await checkLoginStatus();
    }

    btnLogin.addEventListener('click', startLogin);
    btnLoginDone.addEventListener('click', confirmLogin);
    btnSearch.addEventListener('click', startSearch);
    btnCopyLogs.addEventListener('click', () => copyLog(logView, btnCopyLogs));
    btnOllamaLogs.addEventListener('click', loadOllamaLogs);
    btnCopyOllamaLogs.addEventListener('click', () => copyLog(ollamaLogView, btnCopyOllamaLogs));
    document.getElementById('btn-save-config').addEventListener('click', saveConfig);
    document.getElementById('btn-save-env').addEventListener('click', saveEnv);
    document.getElementById('btn-save-resume').addEventListener('click', saveResume);
    document.getElementById('btn-save-qa').addEventListener('click', saveQA);

    loadConfig();
    loadEnv();
    loadResume();
    loadQA();
    checkLoginStatus();
    setInterval(pollStatus, 1500);
})();

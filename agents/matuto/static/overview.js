'use strict';

const overviewPage = () => (window.location.hash || '').replace(/^#\/?/, '').split('/')[0] || 'overview';
let overviewRequest = 0;

async function refreshOverview() {
    const request = ++overviewRequest;
    const tasks = document.getElementById('overview-setup-tasks');
    const configured = document.getElementById('overview-configured');
    const entries = document.getElementById('overview-configured-list');
    const item = (record, done) => {
        const row = document.createElement('li');
        if (done) {
            row.textContent = record.label + ': ' + record.value;
            return row;
        }
        const link = document.createElement('a');
        link.href = record.href;
        link.textContent = record.label + ' — ' + record.destination;
        row.append(link);
        return row;
    };
    try {
        const data = await api.request('/api/overview');
        if (request !== overviewRequest) return;
        tasks.replaceChildren(...data.tasks.map(record => item(record, false)));
        tasks.hidden = data.tasks.length === 0;
        entries.replaceChildren(...data.configured.map(record => item(record, true)));
        configured.hidden = data.configured.length === 0;
        if (data.storageRepair) {
            notice('overview', 'storage-repair', 'repair', data.storageRepair.title,
                data.storageRepair.detail,
                { href: data.storageRepair.link, label: 'Settings › Storage & artifacts' });
        } else {
            clearNotice('overview', 'storage-repair');
        }
        clearNotice('overview', 'setup-data');
    } catch (error) {
        if (request !== overviewRequest) return;
        notice('overview', 'setup-data', 'warning', 'Setup status unavailable',
            'Matuto could not read the setup records. Reload the page to try again.');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    if (overviewPage() === 'overview') return refreshOverview();
});
window.addEventListener('hashchange', () => {
    if (overviewPage() === 'overview') return refreshOverview();
});

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../../matuto/static/auth.js'), 'utf8');

function fixture(action = 'sign-in') {
    let submit, destination, theme = 'light';
    const requests = [];
    const error = { textContent: '', focus() { this.focused = true; } };
    const button = { disabled: false };
    const toggle = { setAttribute(name, value) { this[name] = value; },
        addEventListener(_name, handler) { this.click = handler; } };
    const form = { id: 'login', action: `http://matuto.localhost:8890/${action}`,
        elements: { next: { value: '/profiles' } },
        querySelector() { return button; } };
    let values = { username: 'operator', password: 'wrong', next: '/profiles' };
    const context = {
        document: {
            documentElement: { dataset: {} },
            querySelector(selector) {
                if (selector === 'meta[name="csrf-token"]') return { content: 'csrf-fixture' };
                if (selector === '#themeToggle') return toggle;
                return error;
            },
            addEventListener(_name, handler) { submit = handler; },
            body: { replaceWith(body) { values = body.values; error.textContent = body.error; } },
        },
        localStorage: { getItem() { return theme; }, setItem(_key, value) { theme = value; } },
        location: { href: 'http://matuto.localhost:8890/sign-in',
            pathname: '/profiles', search: '?view=language', hash: '#/bundles',
            assign(value) { destination = value; }, reload() { destination = this.href; } },
        URL,
        FormData: class { [Symbol.iterator]() { return Object.entries(values)[Symbol.iterator](); } },
        DOMParser: class { parseFromString(text) { return { body: JSON.parse(text) }; } },
        fetch: async (url, options) => {
            requests.push({ url, options });
            return fixture.response;
        },
    };
    fixture.response = { redirected: true,
        url: 'http://matuto.localhost:8890/profiles?view=language' };
    vm.runInNewContext(source, context);
    return { context, form, button, error, toggle, requests,
        submit() { return submit({ target: form, preventDefault() {} }); },
        setValues(next) { values = next; }, getValues() { return values; },
        destination() { return destination; }, theme() { return theme; } };
}

async function run() {
    const first = fixture();
    await first.submit();
    const request = first.requests[0];
    assert.equal(request.url, 'http://matuto.localhost:8890/sign-in');
    assert.equal(request.options.headers['X-CSRF-Token'], 'csrf-fixture');
    assert.deepEqual(JSON.parse(request.options.body), {
        username: 'operator', password: 'wrong', next: '/profiles?view=language#/bundles',
    });
    assert.equal(first.destination(), 'http://matuto.localhost:8890/profiles?view=language#/bundles');

    const retry = fixture();
    fixture.response = { redirected: false, headers: { get() { return 'text/html'; } },
        async text() { return JSON.stringify({ values: { username: 'operator', password: '' },
            error: 'The username or password did not match.' }); } };
    await retry.submit();
    assert.equal(retry.getValues().username, 'operator');
    assert.equal(retry.getValues().password, '');
    assert.ok(retry.error.focused);
    assert.equal(retry.button.disabled, false);
    retry.setValues({ username: 'operator', password: 'correct' });
    fixture.response = { redirected: true, url: 'http://matuto.localhost:8890/profiles' };
    await retry.submit();
    assert.equal(retry.requests.length, 2);
    assert.equal(JSON.parse(retry.requests[1].options.body).password, 'correct');
    assert.equal(retry.destination(), 'http://matuto.localhost:8890/profiles#/bundles');

    const requestedSection = fixture();
    requestedSection.context.location.hash = '#/settings/maintenance';
    fixture.response = { redirected: true, url: 'http://matuto.localhost:8890/' };
    await requestedSection.submit();
    assert.equal(requestedSection.destination(), 'http://matuto.localhost:8890/#/settings/maintenance');

    const sameAddress = fixture();
    sameAddress.context.location.href = 'http://matuto.localhost:8890/#/settings/maintenance';
    sameAddress.context.location.pathname = '/';
    sameAddress.context.location.search = '';
    sameAddress.context.location.hash = '#/settings/maintenance';
    let reloaded = false;
    sameAddress.context.location.reload = () => { reloaded = true; };
    fixture.response = { redirected: true, url: 'http://matuto.localhost:8890/' };
    await sameAddress.submit();
    assert.ok(reloaded, 'Signing in at the requested address must reload the protected app');

    const withoutFragment = fixture();
    withoutFragment.context.location.hash = '';
    fixture.response = { redirected: true, url: 'http://matuto.localhost:8890/profiles' };
    await withoutFragment.submit();
    assert.equal(withoutFragment.destination(), 'http://matuto.localhost:8890/profiles');

    const creation = fixture('create-login');
    creation.setValues({ username: 'operator', password: 'secret', confirm: 'other' });
    await creation.submit();
    assert.equal(creation.requests.length, 0);
    assert.ok(creation.error.textContent);
    creation.setValues({ username: 'operator', password: 'secret', confirm: 'secret' });
    await creation.submit();
    assert.equal(creation.requests.length, 1);
    assert.equal(JSON.parse(creation.requests[0].options.body).confirm, 'secret');

    const refusal = fixture();
    fixture.response = { redirected: false, headers: { get() { return 'application/json'; } },
        async json() { return { error: 'forbidden' }; } };
    await refusal.submit();
    assert.equal(refusal.error.textContent, 'forbidden');
    assert.equal(refusal.getValues().username, 'operator');
    assert.equal(refusal.destination(), undefined);
    assert.equal(refusal.button.disabled, false);
    refusal.context.fetch = async () => { throw new Error('offline'); };
    await refusal.submit();
    assert.match(refusal.error.textContent, /not confirmed/);
    assert.equal(refusal.destination(), undefined);

    const themed = fixture('create-login');
    const draft = themed.getValues();
    themed.toggle.click();
    assert.equal(themed.theme(), 'dark');
    assert.equal(themed.context.document.documentElement.dataset.theme, 'dark');
    assert.equal(themed.getValues(), draft);
}

run().catch(error => { console.error(error); process.exitCode = 1; });

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {execFileSync} = require('node:child_process');

const source = fs.readFileSync(path.join(__dirname, '../../matuto/static/account.js'), 'utf8');
const catalogue = execFileSync(process.env.PYTHON || 'python3', ['-B', '-c',
    'from matuto.page.catalogue import SCRIPT; print(SCRIPT)'],
    {cwd:path.resolve(__dirname, '../../'), encoding:'utf8'});
const section = fs.readFileSync(path.join(__dirname, '../../matuto/page/account.py'), 'utf8');
const headerScript = fs.readFileSync(path.join(__dirname, '../../matuto/static/header_status.js'), 'utf8');

function fixture(responses = [], {header = false} = {}) {
    const handlers = {};
    const values = { current_password: 'old', new_password: 'new', confirmation: 'new' };
    const requests = [];
    const error = { textContent: '', focus() { this.focused = true; } };
    const username = { textContent: '' };
    const submitButton = { disabled: false };
    const form = { addEventListener(name, handler) { handlers.form = handler; },
        querySelector() { return submitButton; } };
    const signOut = { disabled: false, addEventListener(_name, handler) { handlers.signOut = handler; } };
    const basicElement = () => ({children:[],attributes:{},textContent:'',parentNode:null,
        addEventListener(){},setAttribute(name,value){this.attributes[name]=String(value);},
        getAttribute(name){return this.attributes[name] ?? null;},
        append(...children){for(const child of children){this.children.push(child);child.parentNode=this;}},
        remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(item=>item!==this);this.parentNode=null;},
        querySelector(selector){const attr=/^\[([^=]+)="([^"]+)"\]$/.exec(selector);
            const match=node=>attr?node.getAttribute(attr[1])===attr[2]:selector.startsWith('.')&&
                (node.getAttribute('class')||'').split(/\s+/).includes(selector.slice(1));
            const walk=node=>{for(const child of node.children){if(match(child))return child;const found=walk(child);if(found)return found;}return null;};
            return walk(this);}});
    const catalogueForm = {elements:{namedItem(){return {value:'',checked:false};}},
        addEventListener(){},querySelector(){return {checked:false};},reset(){}};
    const catalogueElements = {'catalogue-form':catalogueForm,'catalogue-list':basicElement(),
        'catalogue-result':{textContent:''},'catalogue-cancel':{addEventListener(){},hidden:true},
        'catalogue-form-title':{textContent:'Add argument'}};
    const notices = basicElement(); notices.setAttribute('class', 'notices');
    const overview = basicElement(); overview.append(notices);
    const headerSlot = basicElement();
    let destination;
    const location = {hash:'#/overview', pathname:'/', search:'', assign(url) { destination = url; }, replace() {}};
    const document = { querySelector(selector) {
            return ({ '#change-password': form, '#account-error': error,
                '#signed-in-as': username, '#sign-out': signOut,
                '[data-slot="header-status"]': headerSlot })[selector];
        }, getElementById(id) { return catalogueElements[id] || (id === 'page-overview' ? overview : null); },
        createElement() { return basicElement(); } };
    const window = {location, fetch: async (url, options) => {
        requests.push({ url, options });
        const response = responses.shift();
        if (response instanceof Error) throw response;
        return response || { ok: true, redirected: false, async json() {
            return { username: 'operator', csrf: 'csrf-value' };
        } };
    }, matutoPopup:{open(){}}, dispatchEvent(){}, addEventListener(){} };
    const context = {
        document,
        FormData: class { constructor() { this.entries = Object.entries(values); }
            [Symbol.iterator]() { return this.entries[Symbol.iterator](); } },
        location, window, AbortController, setTimeout, clearTimeout,
        api: {async request() { return {entries:[]}; }},
    };
    Object.defineProperty(context, 'fetch', {get() { return window.fetch; }});
    if (header) vm.runInNewContext(headerScript.replace('headerStatus.poll();', ''), context);
    vm.runInNewContext(source + '\n' + catalogue, context);
    return { handlers, values, requests, error, username, submitButton, signOut, notices,
        destination: () => destination };
}

async function run() {
    assert.match(section, /TITLE = "Account"/);
    assert.doesNotMatch(section, /name="username"|Change login|Log out/);
    assert.match(section, /Signing out ends this browser's session only/);
    assert.match(section, /changing your password ends every session/i);

    const failed = fixture([{ ok: true, async json() {
        return { username: 'operator', csrf: 'csrf-value' };
    } }, { ok: false, async json() { return { error: 'The current password is wrong.' }; } }]);
    await new Promise(resolve => setImmediate(resolve));
    await failed.handlers.form({ preventDefault() {} });
    assert.equal(failed.requests[1].url, '/api/account/change-password');
    assert.equal(failed.requests[1].options.headers['X-CSRF-Token'], 'csrf-value');
    assert.deepEqual(JSON.parse(failed.requests[1].options.body), {
        current_password: 'old', new_password: 'new', confirmation: 'new',
    });
    assert.equal(failed.error.textContent, 'The current password is wrong.');
    assert.equal(failed.values.new_password, 'new');
    assert.equal(failed.submitButton.disabled, false);

    const empty = fixture();
    await new Promise(resolve => setImmediate(resolve));
    empty.values.new_password = '';
    await empty.handlers.form({ preventDefault() {} });
    assert.equal(empty.error.textContent, 'A new password is required.');
    assert.equal(empty.requests.length, 1);

    const mismatch = fixture();
    await new Promise(resolve => setImmediate(resolve));
    mismatch.values.confirmation = 'different';
    await mismatch.handlers.form({ preventDefault() {} });
    assert.equal(mismatch.error.textContent, 'The passwords do not match.');
    assert.equal(mismatch.requests.length, 1);

    const changed = fixture([{ ok: true, async json() {
        return { username: 'operator', csrf: 'csrf-value' };
    } }, { ok: true, redirected: true, url: '/sign-in?password_changed=1',
        clone() { return { async text() { return ''; } }; } }], {header:true});
    await new Promise(resolve => setImmediate(resolve));
    await changed.handlers.form({ preventDefault() {} });
    assert.equal(changed.destination(), '/sign-in?password_changed=1');

    const signedOut = fixture([{ ok: true, async json() {
        return { username: 'operator', csrf: 'csrf-value' };
    } }, { ok: true, headers:{'content-type':'text/html'},
        clone() { return { async text() { return '<!doctype html><p>signed out</p>'; } }; } }], {header:true});
    await new Promise(resolve => setImmediate(resolve));
    await signedOut.handlers.signOut();
    assert.equal(signedOut.requests[1].url, '/sign-out');
    assert.equal(signedOut.requests[1].options.headers['X-CSRF-Token'], 'csrf-value');
    assert.equal(signedOut.destination(), '/sign-in?signed_out=1');
    assert.equal(signedOut.notices.children.length, 0,
        'a complete HTML sign-out reply stays confirmed through the header extension');
}

run().catch(error => { console.error(error); process.exitCode = 1; });

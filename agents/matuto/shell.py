"""The shell every page shares: skip link, page list, narrow menu and the shared slots.

A page's content is added under `page/`, one file per section, so filling a page never edits
this file; the shell's own shared slots are built here; see README.md. Serving the shell is a
route, which is the HTTP guard's work.
"""

import html
import json

from matuto.discovery import item_names, load_item

PAGES = [("overview", "Overview"), ("model-files", "Model files"), ("profiles", "Profiles"),
         ("runs", "Runs & jobs"), ("runtime", "Runtime"), ("settings", "Settings"),
         ("console", "Model console")]
CONSOLE_HINT = "needs agentic serving"
SUN = ('<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/>'
       '<path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6'
       'M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/></svg>')
MOON = '<svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg>'
THEME_SWITCH = ('<button type="button" id="themeToggle" class="theme-switch" role="switch" '
                'aria-checked="false" aria-label="Dark mode" title="Dark mode">'
                '<span class="track" aria-hidden="true">' + SUN + MOON +
                '<span class="knob"><span class="sun">' + SUN + '</span>'
                '<span class="moon">' + MOON + '</span></span></span></button>')

STYLE = """
:root{color-scheme:light;--bg:#f8f7f3;--panel:#fffefa;--panel2:#eeebe4;--matuto-text:#262524;--matuto-muted:#66605c;--line:#cbc3b8;--accent:#843b44;--ink:#fff;--focus:#315b91;font:16px/1.5 system-ui,sans-serif}
html[data-theme=dark]{color-scheme:dark;--bg:#271f24;--panel:#352b32;--panel2:#42343c;--matuto-text:#f5eae1;--matuto-muted:#cebcb4;--line:#72616a;--accent:#efb69c;--ink:#362321;--focus:#f4d7bb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--matuto-text)}
button{font:inherit;min-height:44px;padding:.5rem .85rem;border:1px solid var(--line);border-radius:.55rem;background:var(--panel);color:var(--matuto-text);cursor:pointer}
/* One visible outline for every Tab stop, at least 3:1 against both shell backgrounds. */
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.skip{position:absolute;top:-5rem;left:1rem}.skip:focus{top:1rem;min-height:44px;display:inline-flex;align-items:center;background:var(--panel);padding:1rem;z-index:5}
.top{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.5rem 1rem;max-width:82rem;margin:auto;padding:1rem clamp(1rem,3vw,2.5rem);border-bottom:1px solid var(--line)}
h1{margin:0;font-size:1.35rem;letter-spacing:-.03em}
.status{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;min-height:32px;font-size:.85rem;color:var(--matuto-muted)}
.tools{display:flex;gap:.5rem;align-items:center}
.icon-btn{width:44px;height:44px;padding:0;font-size:1.1rem}
.theme-switch{min-height:44px;padding:5px;border-radius:999px;background:transparent}
.theme-switch .track{position:relative;display:flex;align-items:center;justify-content:space-between;width:62px;height:32px;padding:0 7px;border-radius:999px;background:var(--panel2)}
.theme-switch svg{width:17px;height:17px;fill:none;stroke:var(--matuto-muted);stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.theme-switch .knob{position:absolute;top:3px;left:3px;display:flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:50%;background:var(--accent);transition:transform .22s}
.theme-switch .knob svg{stroke:var(--ink)}
.theme-switch .moon,.theme-switch[aria-checked=true] .sun{display:none}
.theme-switch[aria-checked=true] .moon{display:inline-flex}
.theme-switch[aria-checked=true] .knob{transform:translateX(30px)}
@media(prefers-reduced-motion:reduce){.theme-switch .knob{transition:none}}
.studio-shell{display:grid;grid-template-columns:12rem minmax(0,1fr);gap:2.5rem;max-width:82rem;margin:auto;padding:1.4rem clamp(1rem,3vw,2.5rem)}
.rail{display:flex;flex-direction:column;gap:.25rem;align-self:start}
.rail a{display:flex;flex-direction:column;justify-content:center;min-height:44px;padding:.55rem 1rem;border-radius:.55rem;color:var(--matuto-muted);text-decoration:none;font-size:.92rem;line-height:1.35}
.rail a:hover{background:var(--panel2);color:var(--matuto-text)}
.rail a[aria-current=page]{background:var(--panel);color:var(--accent);font-weight:600}
.rail a .hint{font-size:.75rem;color:var(--accent)}
main{min-width:0}
.page-heading{margin:0 0 1.2rem;font-size:2rem;letter-spacing:-.035em;line-height:1.2}
.page-heading:focus{outline:none}.page-heading:focus-visible{outline:3px solid var(--focus);outline-offset:3px}
.section-nav{display:flex;flex-wrap:wrap;gap:.3rem;padding:.3rem;border:1px solid var(--line);border-radius:.7rem;background:var(--panel);margin:0 0 1.3rem}
.section-nav a{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:.4rem .8rem;border-radius:.45rem;color:var(--matuto-muted);text-decoration:none;flex:1 1 auto;white-space:nowrap;font-size:.9rem}
.section-nav a:hover{background:var(--panel2)}
/* The current tab is a palette-derived fill that adds no border and no heavier label, so marking it never shifts its text. */
.section-nav a[aria-current=page]{background:color-mix(in srgb,var(--accent) 10%,var(--panel));color:var(--accent)}
#languageProfiles{display:grid;gap:1rem}#languageProfiles label{display:grid;gap:.25rem;margin:.7rem 0}
#importExport #exportRecordList label{display:flex;align-items:flex-start;gap:.5rem;overflow-wrap:anywhere}
#importExport #exportRecordList input{flex:0 0 auto}
#languageProfiles input,#languageProfiles select,#languageProfiles textarea{font:inherit;min-height:44px;max-width:100%;padding:.45rem}
#languageProfiles textarea{min-height:5rem}#languageProfileForm{max-width:48rem}
#argument-catalogue form{display:grid;gap:.6rem;max-width:48rem;margin-top:1rem;padding-top:1rem;border-top:1px solid var(--line)}
#argument-catalogue form label{display:grid;gap:.25rem}
#argument-catalogue input:not([type=checkbox]){box-sizing:border-box;width:100%;min-width:0;min-height:44px;padding:.45rem;border:1px solid var(--matuto-muted);border-radius:.45rem;background:var(--panel);color:var(--matuto-text);font:inherit}
#argument-catalogue fieldset{display:flex;flex-wrap:wrap;gap:.4rem 1rem}
#argument-catalogue fieldset label,#argument-catalogue .takes-value{display:flex;align-items:center;min-height:44px;gap:.4rem}
#argument-catalogue .catalogue-entry{padding:.8rem 0}
.language-profile-record{padding:.8rem 0;border-bottom:1px solid var(--line)}
.language-profile-record h4{margin:0}.language-profile-record ul{display:flex;flex-wrap:wrap;gap:.5rem 1.5rem;margin:.4rem 0;padding-left:1.2rem}
.argument-row{display:grid;grid-template-columns:minmax(9rem,1fr) minmax(7rem,1fr) minmax(7rem,1fr) auto;gap:.4rem;margin:.4rem 0}
.argument-row [hidden]{display:none}
.section-nav:empty,.notices:empty,.status:empty,[data-slot]:empty{display:none}
/* Notice kinds differ in their words first; the border and its colour only echo that. */
.notice{background:var(--panel2);border:1px solid var(--line);border-left-width:4px;border-radius:.6rem;padding:.8rem 1rem;margin:0 0 .8rem;font-size:.9rem}
/* A configured path or a long reason may hold one unbroken run; it wraps rather than widen the page. */
.notice,.reason{overflow-wrap:anywhere}
#overview-configured li{overflow-wrap:anywhere}
.account-username{overflow-wrap:anywhere}
.notice.blocker{border-left-color:var(--accent)}
.notice.warning{border-left-color:var(--matuto-muted)}
.notice.repair{border-left-color:var(--accent);border-left-style:dashed}
.notice p{margin:.3rem 0}
.notice a{display:inline-flex;align-items:center;min-height:44px;margin-top:.45rem;color:var(--accent)}
.runs-log{max-width:100%;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}
.build-pin{overflow-wrap:anywhere}
.runs-evidence{display:block;overflow-wrap:anywhere;margin:.5rem 0}
.runs-acceptance{overflow-wrap:anywhere}
.runs-evidence[hidden]{display:none}
.reason{display:block;font-size:.82rem;color:var(--matuto-muted);margin-top:.25rem}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:1rem;padding:1.3rem;margin:0 0 1.2rem;min-width:0}
.account form{display:grid;gap:.6rem}
.account input{box-sizing:border-box;width:100%;min-width:0;font:inherit;min-height:44px;padding:.6rem;border:1px solid var(--matuto-muted);border-radius:.55rem;background:var(--panel);color:var(--matuto-text)}
/* The one popup every page uses: its width is bounded so no popup overflows a narrow window. */
dialog.popup{width:min(38rem,calc(100vw - 2rem));max-height:85vh;overflow:hidden;margin:auto;padding:1.4rem;border:1px solid var(--line);border-radius:1rem;background:var(--panel);color:var(--matuto-text)}
dialog.popup[open]{display:flex;flex-direction:column}
.popup-body{min-height:0;overflow:auto}
dialog.popup::backdrop{background:color-mix(in srgb,var(--bg) 70%,transparent)}
dialog.popup h2{margin:0 0 .9rem;font-size:1.35rem;letter-spacing:-.03em}
/* A popup failure is one tinted alert below the title, replacing whatever was there. */
.popup-alert{margin:0 0 1rem;padding:.7rem .8rem;border:1px solid color-mix(in srgb,var(--accent) 55%,var(--panel));border-radius:.6rem;background:color-mix(in srgb,var(--accent) 12%,var(--panel))}
.consequence{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:.45rem 1rem;margin:0}
.consequence dt{font-weight:600}
.consequence dd{margin:0}
.consequence ul{margin:0;padding-left:1.1rem}
.popup-actions{display:flex;flex-wrap:wrap;gap:.5rem;justify-content:flex-end;margin-top:1.2rem}
@media(max-width:44.99rem){
 .studio-shell{grid-template-columns:1fr;gap:1rem}
 .pages-toggle{display:inline-flex}
 .rail{display:none;padding-bottom:.8rem;border-bottom:1px solid var(--line)}
 .rail.open{display:flex}
 .page-heading{font-size:1.65rem}
 .section-nav a{flex:1 1 calc(50% - .3rem);white-space:normal;text-align:center}
 .argument-row{grid-template-columns:1fr}
 .argument-row button{width:100%}
 .consequence{grid-template-columns:1fr;gap:.15rem}
 .consequence dd{margin-bottom:.4rem}
 .popup-actions button{flex:1 1 auto}
}
@media(min-width:45rem){.pages-toggle{display:none}}
a:not([hidden]){display:inline-flex;align-items:center}
a,button,input,select,textarea,summary{min-width:44px;min-height:44px}
"""

SCRIPT = """
'use strict';
const CSRF=__CSRF__, STATUS=__STATUS__;
window.matutoCsrf=CSRF;
const $=id=>document.getElementById(id);

/* The light/dark switch: the choice lives in this browser only, and a first visit is light.
   A browser that refuses storage still starts light and still toggles the page. */
const root=document.documentElement, themeToggle=$('themeToggle');
function storedTheme(){try{return localStorage.getItem('matuto-theme')==='dark'?'dark':'light'}catch(cause){return 'light'}}
function applyTheme(mode){if(root&&root.dataset)root.dataset.theme=mode;if(themeToggle)themeToggle.setAttribute('aria-checked',String(mode==='dark'))}
let theme=storedTheme();
applyTheme(theme);
if(themeToggle)themeToggle.addEventListener('click',()=>{theme=theme==='dark'?'light':'dark';try{localStorage.setItem('matuto-theme',theme)}catch(cause){}applyTheme(theme)});

/* Every mutation carries the token the HTTP guard checks, and a refused or unreachable request
   raises the error its caller shows instead of resolving as a success. */
const api={async request(path,{method='GET',body,rawBody}={}){
  const headers={};
  if(method!=='GET'&&method!=='HEAD'){headers['Content-Type']='application/json';headers['X-CSRF-Token']=CSRF}
  let response;
  try{response=await fetch(path,{method,cache:'no-store',headers,
    body:rawBody===undefined?(body===undefined?undefined:JSON.stringify(body)):rawBody})}
  catch(cause){throw new Error('Matuto is not answering.')}
  let payload=null;
  try{payload=await response.json()}catch(cause){payload=null}
  if(!response.ok){const detail=payload&&typeof payload.error==='string'?payload.error:'';throw Object.assign(new Error(detail||'The request was refused.'),{detail,blockers:payload&&payload.blockers||[]})}
  return payload;
}};

/* The one popup component every page uses. It fills the shell's reserved popup slot with a
   single dialog built on first use, so an alert can only ever belong to the popup open now. */
const popup=(()=>{
  const slot=document.querySelector('[data-slot="popup"]');
  const GROUPS=[['Stops','stops'],['Starts','starts'],['Stays','stays']];
  let dialog,heading,alert,body,actions,opener,returnFocus,invocation=0;

  function part(tag,attributes,text){
    const node=document.createElement(tag);
    for(const name in attributes)node.setAttribute(name,attributes[name]);
    if(text!==undefined)node.textContent=text;
    return node;
  }
  function button(label,handler){
    const node=part('button',{type:'button'},label);
    node.addEventListener('click',handler);
    return node;
  }
  /* Stops, Starts and Stays are the caller's data; an empty group reads as Nothing. */
  function consequence(groups){
    const list=part('dl',{'class':'consequence'});
    for(const [label,key] of GROUPS){
      const items=groups[key]||[];
      const detail=part('dd',{});
      if(items.length){
        const entries=part('ul',{});
        for(const item of items)entries.append(part('li',{},String(item)));
        detail.append(entries);
      }else detail.textContent='Nothing';
      list.append(part('dt',{},label),detail);
    }
    return list;
  }
  function build(){
    heading=part('h2',{id:'popupTitle'},'');
    alert=part('div',{'class':'popup-alert',role:'alert',tabindex:'-1'});
    alert.hidden=true;
    body=part('div',{'class':'popup-body'});
    actions=part('div',{'class':'popup-actions'});
    dialog=part('dialog',{'class':'popup','aria-labelledby':'popupTitle'});
    dialog.append(heading,body,actions);
    body.append(alert);
    /* Closing without acting restores the opener or its refreshed replacement and clears the alert. */
    dialog.addEventListener('close',()=>{
      const back=opener;
      const target=(returnFocus&&returnFocus())||back;
      opener=null;
      returnFocus=null;
      invocation+=1;
      alert.textContent='';
      alert.hidden=true;
      slot.hidden=true;
      if(target)target.focus();
    });
    slot.append(dialog);
  }
  /* A refusal or failure stays in this popup: one focused alert below the title that replaces
     the last one, with the review context and the operator's input left as they were. */
  function fail(error){
    if(!dialog||!dialog.open)return;
    alert.textContent=error&&error.message?error.message:String(error);
    for(const blocker of (error&&Array.isArray(error.blockers)?error.blockers:[])){
      const row=part('p',{},String(blocker.text||'Install blocker'));
      if(blocker.href){const link=part('a',{href:blocker.href},String(blocker.label||'Open blocker'));
        row.append(document.createTextNode(' — '),link)}
      alert.append(row);
    }
    alert.hidden=false;
    alert.focus();
    alert.scrollIntoView();
  }
  function close(){if(dialog&&dialog.open)dialog.close()}
  /* Confirming is a separate deliberate action: opening the popup runs nothing, and a refused
     or failed confirmation leaves it open instead of closing it as though it had succeeded. */
  function open({title,groups,content,safe='Cancel',confirm,confirmAction,returnFocus:restoreFocus}){
    if(!dialog)build();
    opener=document.activeElement;
    returnFocus=restoreFocus;
    invocation+=1;
    heading.textContent=title;
    body.textContent='';
    body.append(alert);
    if(groups)body.append(consequence(groups));
    if(content)body.append(content);
    actions.textContent='';
    alert.textContent='';
    alert.hidden=true;
    const safeChoice=button(safe,close);
    actions.append(safeChoice);
    if(confirm){
      /* One confirmation at a time, and one popup at a time: a second click must not dispatch a
         second mutation, and a settlement may only touch the invocation that started it, so a
         request left pending across a dismissal can never reach the next popup. */
      const action=button(confirm,async()=>{
        if(action.disabled)return;
        action.disabled=true;
        const mine=invocation;
        try{
          await confirmAction();
          if(mine===invocation)close();
        }catch(error){
          action.disabled=false;
          if(mine===invocation)fail(error);
        }
      });
      actions.append(action);
    }
    slot.hidden=false;
    dialog.showModal();
    safeChoice.focus();
  }
  function preview({title,stops,starts,stays,content,safe,confirm,confirmAction,returnFocus}){
    open({title,groups:{stops,starts,stays},content,safe:safe||'Cancel',confirm,confirmAction,returnFocus});
  }
  document.addEventListener('keydown',event=>{if(event.key==='Escape')close()});
  return {open,preview};
})();
window.matutoPopup=popup;

const menu={open:false};
const hint=document.querySelector('#pages .hint');

function applyStatus(status){
  status=status||{};
  /* The status and notice components fill the header and notices; the section tabs, theme switch,
     console hint and popup are filled. */
  const record=status&&status.console;
  const unusable=Boolean(record)&&!record.usable;
  hint.hidden=!unusable;
  if(unusable)hint.textContent=__HINT__;
}
window.matutoApplyStatus=applyStatus;

/* Page-wide blockers, warnings and repair notices go in the one region the shell puts under
   each page's heading and section tabs. One key per condition: the same key replaces its
   notice, and `clearNotice` leaves it when the condition has cleared. */
const NOTICE_KINDS={blocker:'Blocker',warning:'Warning',repair:'Repair needed'};

function notice(page,key,kind,title,detail,link){
  const region=document.querySelector('#page-'+page+' .notices');
  if(!region||!NOTICE_KINDS[kind]||!title)return null;
  let node=region.querySelector('[data-notice="'+key+'"]');
  if(!node){node=document.createElement('div');node.setAttribute('data-notice',key);region.append(node)}
  node.setAttribute('class','notice '+kind);
  node.setAttribute('data-kind',kind);
  node.textContent='';
  const head=document.createElement('strong');
  head.textContent=NOTICE_KINDS[kind]+': '+title;
  node.append(head);
  if(detail){const body=document.createElement('p');body.textContent=detail;node.append(body)}
  for(const [index,target] of (Array.isArray(link)?link:link?[link]:[]).entries()){
    if(index){const separator=document.createElement('span');separator.setAttribute('aria-hidden','true');separator.textContent=' · ';node.append(separator)}
    const where=document.createElement('a');where.setAttribute('href',target.href);where.textContent=target.label;node.append(where)
  }
  return node;
}

function clearNotice(page,key){
  const node=document.querySelector('#page-'+page+' .notices [data-notice="'+key+'"]');
  if(node)node.remove();
}

/* A single unavailable control states its reason beside it. The reason is the control's
   accessible description and the control stays focusable, so a page can explain the
   refusal when the control is activated; no reason leaves the control usable again.
   Only the description token this helper owns is added or removed, so any description
   the page gave the control itself stays, and every reason id is one the page has not
   already used, so no other text can become the control's description by accident. */
const reasons=new WeakMap();
let reasonSeq=0;
const describedTokens=control=>(control.getAttribute('aria-describedby')||'').split(/\s+/).filter(Boolean);
function describeWith(control,tokens){
  if(tokens.length)control.setAttribute('aria-describedby',tokens.join(' '));
  else control.removeAttribute('aria-describedby');
}

function reason(control,text){
  let node=reasons.get(control);
  if(!text){
    if(node){
      const id=node.getAttribute('id');
      describeWith(control,describedTokens(control).filter(token=>token!==id));
      node.remove();
    }
    reasons.delete(control);
    control.removeAttribute('aria-disabled');
    return null;
  }
  if(!node){
    const base=control.id?control.id+'-reason':'reason-';
    let id=base+(++reasonSeq);
    while(document.getElementById(id))id=base+(++reasonSeq);
    node=document.createElement('span');
    node.setAttribute('class','reason');
    node.setAttribute('id',id);
    control.after(node);
    /* aria-disabled, never disabled: a disabled control cannot be activated to explain
       its reason, and a page's own disabled attribute is dropped rather than left to
       strand it. */
    control.removeAttribute('disabled');
    describeWith(control,describedTokens(control).concat(id));
    control.setAttribute('aria-disabled','true');
    reasons.set(control,node);
  }
  node.textContent=text;
  return node;
}
window.matutoReason=reason;

function routeFromHash(){
  const path=(window.location.hash||'').replace(/^#\/?/,'').split('?')[0].split('/');
  return{page:path[0]||'',section:path[1]||''};
}

function pageFromHash(){
  const id=routeFromHash().page;
  return document.getElementById('page-'+id)?id:null;
}

/* One tab per discovered section. Marks the routed section current, shows only its panel,
   and falls back to the page's first section when the route names none or an unknown one. */
function showSection(pageId,name){
  const tabs=[...document.querySelectorAll('#page-'+pageId+' .section-nav a')];
  if(!tabs.length)return '';
  const section=tabs.some(tab=>tab.dataset.section===name)?name:tabs[0].dataset.section;
  for(const tab of tabs)
    if(tab.dataset.section===section)tab.setAttribute('aria-current','page');
    else tab.removeAttribute('aria-current');
  for(const panel of document.querySelectorAll('#page-'+pageId+' section[data-section]'))
    panel.hidden=panel.dataset.section!==section;
  return section;
}

function setPage(id,focus=true,section=null){
  for(const page of document.querySelectorAll('.page'))page.hidden=page.id!=='page-'+id;
  for(const link of document.querySelectorAll('#pages a'))
    if(link.dataset.page===id)link.setAttribute('aria-current','page');
    else link.removeAttribute('aria-current');
  showSection(id,section===null?routeFromHash().section:section);
  if(focus)$('heading-'+id).focus();
  return id;
}

function setMenu(open,focusButton=false){
  menu.open=open;
  $('pages').classList.toggle('open',open);
  $('pagesToggle').setAttribute('aria-expanded',String(open));
  $('pagesToggle').textContent=open?'\\u2715':'\\u2630';
  if(!open&&focusButton)$('pagesToggle').focus();
}

$('pagesToggle').addEventListener('click',()=>setMenu(!menu.open));
for(const link of document.querySelectorAll('#pages a'))
  link.addEventListener('click',()=>{setMenu(false);setPage(link.dataset.page)});
for(const tab of document.querySelectorAll('.section-nav a'))
  tab.addEventListener('click',()=>setPage(tab.dataset.page,true,tab.dataset.section));
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&menu.open)setMenu(false,true)});
window.addEventListener('hashchange',()=>{const id=pageFromHash();if(id)setPage(id)});

applyStatus(STATUS);
setPage(pageFromHash()||'overview',false);
"""


def sections_for(page):
    """The section files that declare this page, as the section's ORDER then name."""
    return sorted((section for name in item_names("page")
                   for section in (load_item("page", name),) if section.PAGE == page),
                  key=lambda section: (getattr(section, "ORDER", 0), section.NAME))


def _json(value):
    """JSON for the page's own script: `<` escaped so a value cannot close the element."""
    return json.dumps(value).replace("<", "\\u003c")


def unusable_console(status):
    """The status says the console is unusable when it names a console that is not usable."""
    record = (status or {}).get("console")
    return bool(record) and not record.get("usable")


def _nav(status):
    links = []
    hidden = "" if unusable_console(status) else ' hidden=""'
    for page_id, label in PAGES:
        current = ' aria-current="page"' if page_id == PAGES[0][0] else ""
        hint = f'<small class="hint"{hidden}>{CONSOLE_HINT}</small>' if page_id == "console" else ""
        links.append(f'<a href="#{page_id}" data-page="{page_id}"{current}>{label}{hint}</a>')
    return "".join(links)


def _section_tabs(page_id, label, sections):
    """The page's tab row: one link per section, each its own route."""
    tabs = "".join(
        f'<a href="#/{page_id}/{section.NAME}" data-page="{page_id}" '
        f'data-section="{section.NAME}">{section.TITLE}</a>'
        for section in sections)
    return f'<nav class="section-nav" data-slot="section-tabs" aria-label="{label} sections">{tabs}</nav>'


def _page(page_id, label, status):
    sections = sections_for(page_id)
    tab_sections = sections if all(getattr(section, "TABBED", True) for section in sections) else []
    panels = "".join(
        f'<section class="panel" data-section="{section.NAME}" '
        f'aria-labelledby="section-{page_id}-{section.NAME}">'
        f'<h3 id="section-{page_id}-{section.NAME}">{section.TITLE}</h3>{section.html(status)}</section>'
        for section in sections)
    hidden = "" if page_id == PAGES[0][0] else ' hidden=""'
    return (f'<div class="page" id="page-{page_id}"{hidden}>'
            f'<h2 class="page-heading" id="heading-{page_id}" tabindex="-1">{label}</h2>'
            f"{_section_tabs(page_id, label, tab_sections)}"
            f'<div class="notices" data-slot="notices" role="status" aria-live="polite"></div>'
            f"{panels}</div>")


def render(status, csrf, nonce=None):
    """The whole shell document. A server may nonce its inline script and style."""
    status = status or {}
    nonce_attr = f' nonce="{nonce}"' if nonce else ""
    pages = "".join(_page(page_id, label, status) for page_id, label in PAGES)
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            '<meta name="csrf-token" content="' + html.escape(str(csrf), quote=True) + '">\n'
            "<title>Matuto</title>\n<style" + nonce_attr + ">" + STYLE + "</style>\n</head>\n<body>\n"
            '<a class="skip" href="#main">Skip to content</a>\n'
            '<header><div class="top"><h1>Matuto</h1>'
            '<div class="status" data-slot="header-status" role="status" aria-live="polite"></div>'
            '<div class="tools">'
            '<button type="button" id="pagesToggle" class="icon-btn pages-toggle" '
            'aria-label="Pages" aria-controls="pages" aria-expanded="false">\u2630</button>'
            '<span data-slot="theme-switch">' + THEME_SWITCH + '</span></div></div></header>\n'
            '<div class="studio-shell">\n<nav id="pages" class="rail" aria-label="Pages">'
            + _nav(status) + "</nav>\n<main id=\"main\" tabindex=\"-1\">\n"
            + pages + "\n</main>\n</div>\n"
            '<div data-slot="popup" hidden=""></div>\n'
            '<script' + nonce_attr + '>\n' + SCRIPT.replace("__CSRF__", _json(csrf))
            .replace("__STATUS__", _json(status)).replace("__HINT__", _json(CONSOLE_HINT))
            + "</script>\n</body>\n</html>\n")
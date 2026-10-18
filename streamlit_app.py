from pathlib import Path
import hmac
import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title='PHID | Public Health Intelligence Dashboard', page_icon='🔬', layout='wide', initial_sidebar_state='collapsed')

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
TGA_FILE = DATA_DIR / 'tga_tableau.csv'
GLOBAL_FILE = DATA_DIR / 'global_shortage_records.csv'
TRENDS_FILE = DATA_DIR / 'global_shortage_trends.csv'

TEAL = '#12d8c8'; TEAL_DARK = '#16746f'; BG = '#070b11'; PANEL = '#0f1724'; PANEL2 = '#121d2c'; BORDER = '#263244'; TEXT = '#d7dee9'; MUTED = '#71809a'

st.markdown(f'''
<style>
html, body, [class*="css"] {{font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;}}
.stApp {{color:{TEXT}; background-color:{BG}; background-image:linear-gradient(rgba(26,42,55,.35) 1px,transparent 1px),linear-gradient(90deg,rgba(26,42,55,.35) 1px,transparent 1px); background-size:80px 80px;}}
[data-testid="stHeader"] {{background:transparent;}} #MainMenu, footer {{visibility:hidden;}}
.block-container {{max-width:1450px; padding-top:2.6rem; padding-bottom:4rem;}}
.brand-wrap {{display:flex;align-items:center;gap:14px;}} .brand-icon {{width:56px;height:56px;display:grid;place-items:center;background:{TEAL};color:#041016;font-size:27px;font-weight:900;}}
.brand {{color:{TEXT};font-size:28px;letter-spacing:.18em;font-weight:800;}} .subtitle {{color:{MUTED};letter-spacing:.18em;font-size:16px;text-transform:uppercase;margin-top:16px;}}
div[data-testid="stForm"] {{background:rgba(15,23,36,.97);border:1px solid {BORDER};border-radius:8px;padding:28px 38px 32px;box-shadow:0 20px 80px rgba(0,0,0,.28);}}
.stTextInput label,.stSelectbox label,.stMultiSelect label {{color:{MUTED}!important;text-transform:uppercase;letter-spacing:.08em;font-size:13px;font-weight:700;}}
.stTextInput input {{background:{PANEL2};color:{TEXT};border:1px solid {BORDER};border-radius:6px;min-height:52px;font-size:16px;}}
.stTextInput input:focus {{border-color:{TEAL};box-shadow:0 0 0 1px {TEAL};}}
.stButton>button,.stFormSubmitButton>button,.stDownloadButton>button {{border-radius:6px;border:1px solid {TEAL_DARK};background:linear-gradient(90deg,#176862,#247d76);color:#061017;font-weight:900;letter-spacing:.12em;text-transform:uppercase;}}
.stButton>button:hover,.stFormSubmitButton>button:hover,.stDownloadButton>button:hover {{border-color:{TEAL};background:{TEAL};color:#041016;}}
.topbar {{background:rgba(10,16,24,.9);border:1px solid {BORDER};border-radius:8px;padding:16px 20px;margin-bottom:20px;}}
.section-label {{color:{TEAL};letter-spacing:.15em;text-transform:uppercase;font-size:13px;font-weight:800;margin-bottom:4px;}} .page-title {{color:{TEXT};font-size:30px;font-weight:800;letter-spacing:.04em;margin-bottom:3px;}} .page-subtitle {{color:{MUTED};font-size:13px;}}
.metric-card {{background:rgba(15,23,36,.96);border:1px solid {BORDER};border-radius:8px;padding:20px 22px;min-height:122px;}} .metric-label {{color:{MUTED};font-size:12px;text-transform:uppercase;letter-spacing:.12em;margin-bottom:12px;}} .metric-value {{color:{TEXT};font-size:31px;font-weight:800;}} .metric-accent {{color:{TEAL};}}
[data-testid="stDataFrame"] {{border:1px solid {BORDER};border-radius:8px;overflow:hidden;}} div[data-baseweb="select"]>div {{background:{PANEL2};border-color:{BORDER};}}
.stTabs [data-baseweb="tab-list"] {{gap:10px;}} .stTabs [data-baseweb="tab"] {{color:{MUTED};background:{PANEL};border:1px solid {BORDER};border-radius:6px 6px 0 0;padding:10px 18px;}} .stTabs [aria-selected="true"] {{color:{TEAL}!important;border-color:{TEAL}!important;}}
</style>
''', unsafe_allow_html=True)

@st.cache_data(show_spinner=False)
def load_csv(path):
    path = Path(path)
    if not path.exists(): return pd.DataFrame()
    df = pd.read_csv(path, low_memory=False)
    for c in ['start_date','expected_end_date','resolution_date','first_published_date','last_updated_date','supply_start_date','supply_end_date','last_updated','month']:
        if c in df.columns: df[c] = pd.to_datetime(df[c], errors='coerce')
    return df

def find_col(df,*options):
    return next((c for c in options if c in df.columns), None)

def count_value(df,col,value):
    if not col or df.empty: return 0
    return int(df[col].fillna('').astype(str).str.casefold().eq(str(value).casefold()).sum())

def metric(label,value,accent=False):
    cls='metric-value metric-accent' if accent else 'metric-value'
    st.markdown(f'<div class="metric-card"><div class="metric-label">{label}</div><div class="{cls}">{value}</div></div>', unsafe_allow_html=True)

def style_fig(fig,height=380):
    fig.update_layout(paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',font=dict(family='Courier New, monospace',color=TEXT),height=height,margin=dict(l=20,r=20,t=45,b=20),legend=dict(bgcolor='rgba(0,0,0,0)',font=dict(color=MUTED)),xaxis=dict(gridcolor='#17212d',linecolor=BORDER,tickfont=dict(color=MUTED)),yaxis=dict(gridcolor='#17212d',linecolor=BORDER,tickfont=dict(color=MUTED)))
    return fig

def credentials():
    try:
        return str(st.secrets['auth']['username']), str(st.secrets['auth']['password']), False
    except Exception:
        return 'analyst','PHI2024!',True

def login_page():
    user_expected, pass_expected, fallback = credentials()
    _, centre, _ = st.columns([1.15,2.4,1.15])
    with centre:
        st.markdown('<div style="height:5vh"></div>', unsafe_allow_html=True)
        st.markdown(f'''<div style="display:flex;justify-content:center"><div><div class="brand-wrap" style="justify-content:center"><div class="brand-icon">⚕</div><div class="brand">PHID</div></div><div class="subtitle" style="text-align:center">PUBLIC HEALTH INTELLIGENCE DASHBOARD</div></div></div>''', unsafe_allow_html=True)
        st.markdown('<div style="height:45px"></div>', unsafe_allow_html=True)
        with st.form('login'):
            st.markdown(f'<div style="color:{MUTED};letter-spacing:.16em;text-transform:uppercase;font-size:18px;margin-bottom:12px">▣ &nbsp; SECURE ACCESS</div>', unsafe_allow_html=True)
            username=st.text_input('Username',placeholder='Enter username')
            password=st.text_input('Password',type='password',placeholder='Enter password')
            ok=st.form_submit_button('ACCESS DASHBOARD',use_container_width=True)
        if ok:
            if hmac.compare_digest(username.strip(),user_expected) and hmac.compare_digest(password,pass_expected):
                st.session_state.authenticated=True; st.session_state.username=username.strip(); st.rerun()
            else: st.error('Access denied. Check username and password.')
        demo='<br><span style="opacity:.45">Demo: analyst / PHI2024!</span>' if fallback else ''
        st.markdown(f'<div style="text-align:center;color:{MUTED};margin-top:26px;line-height:1.9;font-size:14px">Restricted system · Authorised personnel only{demo}</div>', unsafe_allow_html=True)

def header():
    c1,c2=st.columns([5,1])
    with c1:
        st.markdown(f'''<div class="topbar"><div class="brand-wrap"><div class="brand-icon" style="width:42px;height:42px;font-size:20px">⚕</div><div><div class="brand" style="font-size:20px">PHID / MED INTEL</div><div class="page-subtitle">Public Health Intelligence Dashboard</div></div></div></div>''',unsafe_allow_html=True)
    with c2:
        st.write('')
        if st.button('LOG OUT',use_container_width=True): st.session_state.authenticated=False; st.rerun()

def overview(global_df,trends_df):
    st.markdown('<div class="section-label">GLOBAL SURVEILLANCE</div><div class="page-title">Medicine Shortage Overview</div><div class="page-subtitle">Interactive reporting across available medicine-shortage regulators.</div>',unsafe_allow_html=True)
    if global_df.empty: st.warning('global_shortage_records.csv not found. Put it in data/.'); return
    rc=find_col(global_df,'regulator'); sc=find_col(global_df,'shortage_status'); mc=find_col(global_df,'medicine_name'); ic=find_col(global_df,'active_ingredient')
    f1,f2,f3=st.columns([1.3,1.3,2.3])
    regs=sorted(global_df[rc].dropna().astype(str).unique()) if rc else []
    stats=sorted(global_df[sc].dropna().astype(str).unique()) if sc else []
    with f1: sel_regs=st.multiselect('Regulator',regs,default=regs)
    with f2: sel_stats=st.multiselect('Status',stats,default=stats)
    with f3: q=st.text_input('Medicine / ingredient search',placeholder='e.g. amoxicillin')
    df=global_df.copy()
    if rc and sel_regs: df=df[df[rc].isin(sel_regs)]
    if sc and sel_stats: df=df[df[sc].isin(sel_stats)]
    if q:
        mask=pd.Series(False,index=df.index)
        for c in [mc,ic]:
            if c: mask |= df[c].fillna('').astype(str).str.contains(q,case=False,regex=False)
        df=df[mask]
    a,b,c,d=st.columns(4)
    with a: metric('Visible records',f'{len(df):,}',True)
    with b: metric('Current',f'{count_value(df,sc,"Current"):,}')
    with c: metric('Unique medicines',f'{df[mc].nunique(dropna=True) if mc else 0:,}')
    with d: metric('Regulators',f'{df[rc].nunique(dropna=True) if rc else 0:,}')
    st.write('')
    g1,g2=st.columns([1,1.35])
    with g1:
        st.markdown('#### STATUS DISTRIBUTION')
        if sc:
            tmp=df.groupby(sc,dropna=False).size().reset_index(name='records').sort_values('records')
            fig=px.bar(tmp,x='records',y=sc,orientation='h',labels={'records':'Records',sc:'Status'}); fig.update_traces(marker_color=TEAL)
            st.plotly_chart(style_fig(fig),use_container_width=True,config={'displaylogo':False})
    with g2:
        st.markdown('#### REPORTING TREND')
        if not trends_df.empty and 'month' in trends_df and 'new_shortage_reports' in trends_df:
            t=trends_df.copy()
            if 'regulator' in t and sel_regs: t=t[t['regulator'].isin(sel_regs)]
            fig=px.line(t,x='month',y='new_shortage_reports',color='regulator' if 'regulator' in t else None,markers=True,labels={'new_shortage_reports':'New reports','month':'Month'})
            st.plotly_chart(style_fig(fig),use_container_width=True,config={'displaylogo':False})
    st.markdown('#### DETAILED RECORDS')
    cols=[c for c in ['medicine_name','active_ingredient','country_region','regulator','shortage_status','manufacturer','start_date','expected_end_date','shortage_reason'] if c in df.columns]
    st.dataframe(df[cols] if cols else df,use_container_width=True,height=430,hide_index=True)
    st.download_button('DOWNLOAD FILTERED RECORDS',df.to_csv(index=False).encode('utf-8'),'medintel_filtered_shortages.csv','text/csv')

def australia(tga_df):
    st.markdown('<div class="section-label">AUSTRALIA / TGA</div><div class="page-title">Australian Medicine Shortages</div><div class="page-subtitle">Current and historical intelligence from the TGA ETL pipeline.</div>',unsafe_allow_html=True)
    if tga_df.empty: st.warning('tga_tableau.csv not found. Put it in data/.'); return
    sc=find_col(tga_df,'status_normalised','shortage_status'); ic=find_col(tga_df,'impact_normalised','impact_rating'); ac=find_col(tga_df,'availability_normalised','availability'); mc=find_col(tga_df,'artg_name','medicine_name'); ing=find_col(tga_df,'active_ingredients','active_ingredient')
    f1,f2,f3,f4=st.columns([1.2,1.2,1.2,2.2])
    with f1: s=st.multiselect('Status',sorted(tga_df[sc].dropna().astype(str).unique()) if sc else [],default=sorted(tga_df[sc].dropna().astype(str).unique()) if sc else [])
    with f2: i=st.multiselect('Impact',sorted(tga_df[ic].dropna().astype(str).unique()) if ic else [],default=sorted(tga_df[ic].dropna().astype(str).unique()) if ic else [])
    with f3: a=st.multiselect('Availability',sorted(tga_df[ac].dropna().astype(str).unique()) if ac else [],default=sorted(tga_df[ac].dropna().astype(str).unique()) if ac else [])
    with f4: q=st.text_input('Medicine / ingredient',placeholder='Search TGA medicines...')
    df=tga_df.copy()
    if sc and s: df=df[df[sc].isin(s)]
    if ic and i: df=df[df[ic].isin(i)]
    if ac and a: df=df[df[ac].isin(a)]
    if q:
        mask=pd.Series(False,index=df.index)
        for c in [mc,ing]:
            if c: mask |= df[c].fillna('').astype(str).str.contains(q,case=False,regex=False)
        df=df[mask]
    c1,c2,c3,c4=st.columns(4)
    with c1: metric('Current shortages',f'{count_value(df,sc,"Current"):,}',True)
    with c2: metric('Critical impact',f'{count_value(df,ic,"Critical"):,}')
    with c3: metric('Anticipated',f'{count_value(df,sc,"Anticipated"):,}')
    with c4: metric('Unavailable',f'{count_value(df,ac,"Unavailable"):,}')
    st.write('')
    g1,g2=st.columns(2)
    with g1:
        if ic:
            tmp=df.groupby(ic).size().reset_index(name='records').sort_values('records',ascending=False)
            fig=px.bar(tmp,x=ic,y='records',labels={ic:'Impact','records':'Records'}); fig.update_traces(marker_color=TEAL)
            st.plotly_chart(style_fig(fig),use_container_width=True,config={'displaylogo':False})
    with g2:
        if ac:
            tmp=df.groupby(ac).size().reset_index(name='records').sort_values('records',ascending=False)
            fig=px.bar(tmp,x=ac,y='records',labels={ac:'Availability','records':'Records'}); fig.update_traces(marker_color=TEAL_DARK)
            st.plotly_chart(style_fig(fig),use_container_width=True,config={'displaylogo':False})
    cols=[c for c in [mc,ing,sc,ic,ac,'supply_start_date','supply_end_date','reason','management_action'] if c and c in df.columns]
    st.dataframe(df[cols] if cols else df,use_container_width=True,height=460,hide_index=True)

def explorer(global_df):
    st.markdown('<div class="section-label">DECISION SUPPORT</div><div class="page-title">Medicine Explorer</div><div class="page-subtitle">Search and export records across regulators.</div>',unsafe_allow_html=True)
    if global_df.empty: st.warning('Global shortage records are not available.'); return
    q=st.text_input('Search',placeholder='Medicine, ingredient or manufacturer...')
    df=global_df.copy()
    if q:
        mask=pd.Series(False,index=df.index)
        for c in [x for x in ['medicine_name','active_ingredient','manufacturer'] if x in df.columns]: mask |= df[c].fillna('').astype(str).str.contains(q,case=False,regex=False)
        df=df[mask]
    st.caption(f'{len(df):,} matching records')
    st.dataframe(df,use_container_width=True,height=560,hide_index=True)
    st.download_button('EXPORT SEARCH RESULTS',df.to_csv(index=False).encode('utf-8'),'medicine_explorer_results.csv','text/csv')

if 'authenticated' not in st.session_state: st.session_state.authenticated=False
if not st.session_state.authenticated:
    login_page(); st.stop()

global_df=load_csv(GLOBAL_FILE); trends_df=load_csv(TRENDS_FILE); tga_df=load_csv(TGA_FILE)
header()
t1,t2,t3=st.tabs(['GLOBAL OVERVIEW','AUSTRALIA / TGA','MEDICINE EXPLORER'])
with t1: overview(global_df,trends_df)
with t2: australia(tga_df)
with t3: explorer(global_df)

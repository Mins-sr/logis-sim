import { StrictMode, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type State = {
  session: { id: string; name: string; status: string; speed: number; current_sim_time_ms: number; state_version: number; event_seq: number };
  membership: { role: string; is_host: boolean; is_warehouse_operator: boolean };
  jobs: Array<{ id: string; kind: string; location: string; pallets: number; status: string; revenue_yen: number; version: number }>;
  trucks: Array<{ id: string; name: string; status: string; capacity_pallets: number; assigned_user_id: string | null }>;
  docks: Array<{ id: string; name: string; occupied_by: string | null }>;
  staging_areas: Array<{ id: string; name: string; capacity_pallets: number; used_pallets: number }>;
};

const api = async (path: string, options: RequestInit = {}) => {
  const response = await fetch(path, { credentials: "include", headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
};

function App() {
  const portal = window.location.pathname.startsWith("/driver") ? "ドライバーポータル" : window.location.pathname.startsWith("/shipper") ? "荷主ポータル" : "ホスト管理コンソール";
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState("");
  const [login, setLogin] = useState({ username: "admin", password: "admin" });
  const [plan, setPlan] = useState<any>(null);
  const [lastEvent, setLastEvent] = useState("待機中");

  const sessionId = "demo-session";
  const refresh = async () => {
    try { setState(await api(`/api/v1/sessions/${sessionId}/state`)); setError(""); } catch (e) { setError(String(e)); }
  };
  useEffect(() => { refresh(); const timer = window.setInterval(refresh, 1000); return () => window.clearInterval(timer); }, []);

  useEffect(() => {
    if (!state) return;
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${window.location.host}/ws/v1/sessions/${sessionId}`);
    socket.onmessage = (message) => { const value = JSON.parse(message.data); if (value.type !== "heartbeat") setLastEvent(`${value.type} / seq ${value.seq}`); };
    return () => socket.close();
  }, [Boolean(state)]);

  const runLogin = async () => { try { await api("/api/v1/auth/login", { method: "POST", body: JSON.stringify(login) }); await refresh(); } catch (e) { setError(String(e)); } };
  const createPlan = async () => { try { const value = await api(`/api/v1/sessions/${sessionId}/plans`, { method: "POST", body: JSON.stringify({}) }); setPlan(value); } catch (e) { setError(String(e)); } };
  const sendSpeed = async (speed: number) => { try { await api(`/api/v1/sessions/${sessionId}/commands`, { method: "POST", body: JSON.stringify({ type: "SET_SPEED", idempotency_key: `speed-${speed}-${Date.now()}`, payload: { speed } }) }); } catch (e) { setError(String(e)); } };
  const clock = useMemo(() => Math.floor((state?.session.current_sim_time_ms || 0) / 1000), [state]);

  return <main>
    <header><div><span className="eyebrow">SLAPStack / VRPB　·　{portal}</span><h1>物流シミュレーション基盤</h1><p>帰り荷計画と多人数参加型の倉庫運営</p></div><div className="login"><input value={login.username} onChange={e => setLogin({ ...login, username: e.target.value })} /><input type="password" value={login.password} onChange={e => setLogin({ ...login, password: e.target.value })} /><button onClick={runLogin}>ログイン</button></div></header>
    {error && <div className="error">{error}</div>}
    {!state ? <section className="empty"><h2>デモセッションに接続してください</h2><button onClick={runLogin}>接続</button></section> : <>
      <section className="hero"><div><span className="pill">{state.session.status}</span><h2>{state.session.name}</h2><p>役割: {state.membership.role}　|　シミュレーション時刻: {clock.toLocaleString()}秒　|　状態版: {state.session.state_version}</p></div><div className="controls"><button onClick={() => sendSpeed(0)}>停止</button><button onClick={() => sendSpeed(1)}>1倍</button><button onClick={() => sendSpeed(10)}>10倍</button><button onClick={() => sendSpeed(60)}>60倍</button></div></section>
      <section className="grid stats"><article><span>公開案件</span><strong>{state.jobs.filter(j => j.status === "OPEN").length}</strong><small>全 {state.jobs.length} 件</small></article><article><span>稼働トラック</span><strong>{state.trucks.filter(t => t.status !== "AVAILABLE").length}/{state.trucks.length}</strong><small>車両状態</small></article><article><span>バース</span><strong>{state.docks.filter(d => d.occupied_by).length}/{state.docks.length}</strong><small>占有状況</small></article><article><span>直近通知</span><strong className="small-value">{lastEvent}</strong><small>WebSocket</small></article></section>
      <section className="grid main-grid"><article className="panel wide"><div className="panel-title"><h3>案件市場</h3><button onClick={createPlan}>VRPB計画を生成</button></div><table><thead><tr><th>種別</th><th>地点</th><th>数量</th><th>報酬</th><th>状態</th></tr></thead><tbody>{state.jobs.map(job => <tr key={job.id}><td><span className={`type type-${job.kind}`}>{job.kind === "L" ? "往路" : "帰り荷"}</span></td><td>{job.location}</td><td>{job.pallets} pallet</td><td>¥{job.revenue_yen.toLocaleString()}</td><td>{job.status}</td></tr>)}</tbody></table>{plan && <pre className="plan">{JSON.stringify(plan, null, 2)}</pre>}</article><article className="panel"><h3>庫内リソース</h3>{state.staging_areas.map(area => <div className="resource" key={area.id}><div><span>{area.name}</span><b>{area.used_pallets}/{area.capacity_pallets} pallet</b></div><div className="bar"><i style={{ width: `${Math.min(100, area.used_pallets / area.capacity_pallets * 100)}%` }} /></div></div>)}{state.docks.map(dock => <div className="dock" key={dock.id}><span>{dock.name}</span><b className={dock.occupied_by ? "busy" : "free"}>{dock.occupied_by ? "使用中" : "空き"}</b></div>)}</article></section>
    </>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);

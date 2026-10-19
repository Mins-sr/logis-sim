import { StrictMode, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type State = {
  session: { id: string; name: string; status: string; speed: number; current_sim_time_ms: number; state_version: number; event_seq: number };
  membership: { role: string; is_host: boolean; is_warehouse_operator: boolean };
  jobs: Array<{ id: string; kind: string; location: string; pallets: number; status: string; revenue_yen: number; version: number }>;
  trucks: Array<{ id: string; name: string; status: string; capacity_pallets: number; assigned_user_id: string | null }>;
  docks: Array<{ id: string; name: string; occupied_by: string | null }>;
  staging_areas: Array<{ id: string; name: string; capacity_pallets: number; used_pallets: number }>;
  trips: Array<{ id: string; truck_id: string; status: string; phase: string; current_job_id: string | null; next_event_at_ms: number | null; wait_reason: string | null; progress: { outbound_pallets: number; return_pallets: number } }>;
};

type DemoRole = "HOST" | "SHIPPER" | "DRIVER";
type CommandStatus = { command_id: string; status: string; rejection_reason?: string | null; result?: unknown };
const demoRoles: Array<{ role: DemoRole; label: string; description: string; username: string; password: string }> = [
  { role: "HOST", label: "管理者として開く", description: "シミュレーションの進行、案件、庫内リソースを操作します。", username: "admin", password: "admin" },
  { role: "SHIPPER", label: "荷主として参加", description: "案件市場を確認し、配送案件を登録します。", username: "shipper", password: "shipper" },
  { role: "DRIVER", label: "ドライバーとして参加", description: "車両を選び、担当する配送案件を確認します。", username: "driver", password: "driver" },
];

const api = async (path: string, options: RequestInit = {}) => {
  const response = await fetch(path, { credentials: "include", headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
};

function App() {
  const pathRole: DemoRole | null = window.location.pathname.startsWith("/driver") ? "DRIVER" : window.location.pathname.startsWith("/shipper") ? "SHIPPER" : window.location.pathname.startsWith("/host") ? "HOST" : null;
  const [selectedRole, setSelectedRole] = useState<DemoRole | null>(pathRole);
  const initialRoleInfo = demoRoles.find(item => item.role === pathRole);
  const [state, setState] = useState<State | null>(null);
  const [authenticated, setAuthenticated] = useState(false);
  const [userId, setUserId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [login, setLogin] = useState(() => ({ username: initialRoleInfo?.username || "admin", password: initialRoleInfo?.password || "admin" }));
  const [plan, setPlan] = useState<any>(null);
  const [lastEvent, setLastEvent] = useState("待機中");
  const [newLocation, setNewLocation] = useState("");
  const [newJobKind, setNewJobKind] = useState<"L" | "B">("B");
  const [selectedTruckId, setSelectedTruckId] = useState("");
  const [loginGeneration, setLoginGeneration] = useState(0);
  const authAttempt = useRef(0);
  const authController = useRef<AbortController | null>(null);
  const refreshController = useRef<AbortController | null>(null);
  const commandControllers = useRef(new Set<AbortController>());
  const tabId = useRef(`tab-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  const authInFlight = useRef(false);

  const abortCommands = () => {
    commandControllers.current.forEach(controller => controller.abort());
    commandControllers.current.clear();
  };

  const roleInfo = demoRoles.find(item => item.role === selectedRole);
  const portal = state?.membership.role === "DRIVER" ? "ドライバーポータル" : state?.membership.role === "SHIPPER" ? "荷主ポータル" : "ホスト管理コンソール";
  const availableTrucks = useMemo(() => state?.trucks.filter(truck => !truck.assigned_user_id || truck.assigned_user_id === userId) || [], [state, userId]);
  const planTrucks = state?.membership.role === "HOST" ? state.trucks : availableTrucks;

  const sessionId = "demo-session";
  const refresh = async (generation = authAttempt.current, signal?: AbortSignal) => {
    if (!authenticated || generation !== authAttempt.current) return;
    try {
      const next = await api(`/api/v1/sessions/${sessionId}/state`, { signal });
      if (generation === authAttempt.current) { setState(next); setError(""); }
    } catch (e) {
      if (generation === authAttempt.current) setError(String(e));
    }
  };
  useEffect(() => {
    if (!selectedRole || !authenticated) return;
    const generation = authAttempt.current;
    const controller = new AbortController();
    refreshController.current = controller;
    void refresh(generation, controller.signal);
    const timer = window.setInterval(() => void refresh(generation, controller.signal), 1000);
    return () => {
      window.clearInterval(timer);
      controller.abort();
      if (refreshController.current === controller) refreshController.current = null;
    };
  }, [selectedRole, authenticated, loginGeneration]);

  useEffect(() => {
    if (!state || !["DRIVER", "HOST"].includes(state.membership.role)) {
      setSelectedTruckId("");
      return;
    }
    setSelectedTruckId(current => planTrucks.some(truck => truck.id === current) ? current : planTrucks[0]?.id || "");
  }, [state, planTrucks]);

  useEffect(() => {
    const channel = "BroadcastChannel" in window ? new BroadcastChannel("tof-logis-role") : null;
    const onMessage = (event: MessageEvent) => {
      if (event.data?.type !== "role-changed" || event.data?.source === tabId.current) return;
      authController.current?.abort();
      refreshController.current?.abort();
      abortCommands();
      authAttempt.current += 1;
      authInFlight.current = false;
      setAuthenticated(false);
      setUserId(null);
      setState(null);
      setPlan(null);
      setSelectedRole(null);
      setSelectedTruckId("");
      setNewLocation("");
      setLastEvent("待機中");
      setNotice("");
      setLoginGeneration(value => value + 1);
      setLogin({ username: "admin", password: "admin" });
      window.history.replaceState({}, "", "/");
      setError("別のタブで役割が切り替わりました。もう一度役割を選んでください。");
    };
    channel?.addEventListener("message", onMessage);
    return () => { channel?.removeEventListener("message", onMessage); channel?.close(); };
  }, []);

  useEffect(() => {
    if (!state || !authenticated) return;
    const generation = authAttempt.current;
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${window.location.host}/ws/v1/sessions/${sessionId}`);
    socket.onmessage = (message) => { const value = JSON.parse(message.data); if (generation === authAttempt.current && value.type !== "heartbeat") setLastEvent(`${value.type} / seq ${value.seq}`); };
    return () => socket.close();
  }, [state?.session.id, authenticated, loginGeneration]);

  const notifyRoleChanged = () => {
    const channel = "BroadcastChannel" in window ? new BroadcastChannel("tof-logis-role") : null;
    channel?.postMessage({ type: "role-changed", source: tabId.current });
    channel?.close();
  };

  const runLogin = async (credentials = login, expectedRole: DemoRole | null = selectedRole) => {
    if (authInFlight.current) return;
    authInFlight.current = true;
    const attempt = ++authAttempt.current;
    authController.current?.abort();
    refreshController.current?.abort();
    abortCommands();
    const controller = new AbortController();
    authController.current = controller;
    setAuthenticated(false);
    setState(null);
    setPlan(null);
    setLoginGeneration(value => value + 1);
    setError("");
    setNotice("");
    try {
      const loginResult = await api("/api/v1/auth/login", { method: "POST", body: JSON.stringify(credentials), signal: controller.signal });
      if (attempt !== authAttempt.current) return;
      notifyRoleChanged();
      setUserId(loginResult.user_id || null);
      const next = await api(`/api/v1/sessions/${loginResult.session_id || sessionId}/state`, { signal: controller.signal }) as State;
      if (attempt !== authAttempt.current) return;
      const actualRole = next.membership.role as DemoRole;
      if (!expectedRole && demoRoles.some(item => item.role === actualRole)) setSelectedRole(actualRole);
      setAuthenticated(true);
      setState(next);
      setLoginGeneration(value => value + 1);
      if (expectedRole && next.membership.role !== expectedRole) setNotice(`ログイン済みユーザーの実際のMembershipは「${next.membership.role}」です。入口の選択は権限を変更しません。`);
    } catch (e) {
      if (attempt === authAttempt.current && (e as Error).name !== "AbortError") setError("ログインできませんでした。パスワードが変更されている場合は、下の入力欄でログインしてください。");
    } finally {
      if (attempt === authAttempt.current) authInFlight.current = false;
    }
  };
  const chooseRole = (role: DemoRole) => {
    authController.current?.abort();
    refreshController.current?.abort();
    abortCommands();
    authAttempt.current += 1;
    authInFlight.current = false;
    const info = demoRoles.find(item => item.role === role)!;
    setAuthenticated(false);
    setUserId(null);
    setState(null);
    setPlan(null);
    setNotice("");
    setSelectedRole(role);
    setLogin({ username: info.username, password: info.password });
    void runLogin({ username: info.username, password: info.password }, role);
  };
  const chooseAnotherRole = () => {
    authController.current?.abort();
    refreshController.current?.abort();
    abortCommands();
    authAttempt.current += 1;
    authInFlight.current = false;
    setAuthenticated(false);
    setUserId(null);
    setState(null);
    setPlan(null);
    setSelectedRole(null);
    setError("");
    setNotice("");
    setSelectedTruckId("");
    setNewLocation("");
    setLastEvent("待機中");
    setLogin({ username: "admin", password: "admin" });
    setLoginGeneration(value => value + 1);
    window.history.replaceState({}, "", "/");
  };
  const createPlan = async () => {
    if (!selectedTruckId) { setError("計画対象の車両を選択してください。"); return; }
    try { const value = await api(`/api/v1/sessions/${sessionId}/plans`, { method: "POST", body: JSON.stringify({ truck_id: selectedTruckId }) }); setPlan({ ...value, id: value.plan_id }); } catch (e) { setError(String(e)); }
  };
  const retrievePlan = async () => {
    const planId = plan?.id || plan?.plan_id;
    if (!planId) { setError("先に計画を生成してください。"); return; }
    try { setPlan(await api(`/api/v1/sessions/${sessionId}/plans/${planId}`)); } catch (e) { setError(String(e)); }
  };
  const startTrip = async () => {
    const planId = plan?.id || plan?.plan_id;
    if (!planId || !selectedTruckId) { setError("計画と車両を選択してください。"); return; }
    await submitCommand("START_TRIP", { type: "START_TRIP", idempotency_key: `start-${planId}-${selectedTruckId}-${Date.now()}`, payload: { plan_id: planId, truck_id: selectedTruckId } });
  };
  const waitForCommand = async (commandId: string, label: string, generation: number, signal: AbortSignal) => {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      if (generation !== authAttempt.current) return;
      try {
        const result = await api(`/api/v1/commands/${commandId}`, { signal }) as CommandStatus;
        if (generation !== authAttempt.current) return;
        if (result.status === "APPLIED") {
          setLastEvent(`${label}: 適用済み`);
          return;
        }
        if (result.status === "REJECTED") {
          const reason = result.rejection_reason || "理由不明";
          setLastEvent(`${label}: 拒否`);
          setError(`${label}が拒否されました: ${reason}`);
          return;
        }
        setLastEvent(`${label}: 処理中`);
      } catch (e) {
        if (generation === authAttempt.current) setError(`${label}の結果を取得できませんでした: ${String(e)}`);
        return;
      }
      await new Promise(resolve => window.setTimeout(resolve, 1000));
    }
    if (generation === authAttempt.current) {
      setLastEvent(`${label}: 結果未確認`);
      setError(`${label}の結果を確認できませんでした（30秒経過）。`);
    }
  };
  const submitCommand = async (label: string, body: Record<string, unknown>, generation = authAttempt.current) => {
    const controller = new AbortController();
    commandControllers.current.add(controller);
    try {
      const result = await api(`/api/v1/sessions/${sessionId}/commands`, { method: "POST", body: JSON.stringify(body), signal: controller.signal }) as CommandStatus;
      if (generation !== authAttempt.current) return;
      setError("");
      setLastEvent(`${label}: 処理中`);
      await waitForCommand(result.command_id, label, generation, controller.signal);
    } catch (e) {
      if (generation === authAttempt.current && (e as Error).name !== "AbortError") setError(String(e));
    } finally {
      commandControllers.current.delete(controller);
    }
  };
  const createJob = async () => {
    if (!newLocation.trim()) { setError("地点を入力してください。"); return; }
    const generation = authAttempt.current;
    const location = newLocation.trim();
    try {
      await submitCommand("案件登録", { type: "CREATE_JOB", idempotency_key: `job-${Date.now()}-${Math.random().toString(36).slice(2)}`, payload: { kind: newJobKind, location, pallets: 1, weight_g: 1000, earliest_ms: 0, latest_ms: 86400000, revenue_yen: 1000 } }, generation);
      if (generation !== authAttempt.current) return;
      setNewLocation("");
    } catch (e) { setError(String(e)); }
  };
  const acceptJob = async (job: State["jobs"][number]) => {
    if (!selectedTruckId) { setError("利用可能な車両を選択してください。"); return; }
    await submitCommand("案件参加", { type: "ACCEPT_JOB", idempotency_key: `accept-${job.id}-${selectedTruckId}-${Date.now()}-${Math.random().toString(36).slice(2)}`, expected_entity_version: job.version, payload: { job_id: job.id, truck_id: selectedTruckId } });
  };
  const sendSessionCommand = async (type: "PAUSE" | "RESUME") => { await submitCommand(type === "PAUSE" ? "一時停止" : "再開", { type, idempotency_key: `${type.toLowerCase()}-${Date.now()}-${Math.random().toString(36).slice(2)}`, payload: {} }); };
  const sendSpeed = async (speed: number) => { await submitCommand("速度変更", { type: "SET_SPEED", idempotency_key: `speed-${speed}-${Date.now()}-${Math.random().toString(36).slice(2)}`, payload: { speed } }); };
  const clock = useMemo(() => Math.floor((state?.session.current_sim_time_ms || 0) / 1000), [state]);

  return <main>
    <header><div><span className="eyebrow">SLAPStack / VRPB　·　{portal}</span><h1>物流シミュレーション基盤</h1><p>帰り荷計画と多人数参加型の倉庫運営</p></div><div className="login"><input value={login.username} onChange={e => setLogin({ ...login, username: e.target.value })} /><input type="password" value={login.password} onChange={e => setLogin({ ...login, password: e.target.value })} /><button onClick={() => runLogin()}>ログイン</button></div></header>
    {error && <div className="error">{error}</div>}{notice && <div className="notice">{notice}</div>}
    {!selectedRole ? <section className="empty"><h2>このシステムを始める</h2><p>役割を選ぶと、デモアカウントでログインして参加できます。</p><div className="role-choices">{demoRoles.map(item => <button className="role-choice" key={item.role} onClick={() => chooseRole(item.role)}><strong>{item.label}</strong><span>{item.description}</span></button>)}</div><small>同じブラウザの別タブで役割を切り替えると、他のタブも入口へ戻ります。並行して試す場合は別ブラウザプロフィールを使ってください。</small></section> : !state ? <section className="empty"><h2>{roleInfo?.label}</h2><p>デモアカウントで接続しています。接続できない場合は上の入力欄でログインしてください。</p><button onClick={() => runLogin()}>接続</button><button onClick={chooseAnotherRole}>役割を選び直す</button></section> : <>
      <section className="hero"><div><span className="pill">{state.session.status}</span><h2>{state.session.name}</h2><p>役割: {state.membership.role}　|　シミュレーション時刻: {clock.toLocaleString()}秒　|　状態版: {state.session.state_version}</p></div><div className="controls">{state.membership.is_host && <><button onClick={() => sendSessionCommand("PAUSE")}>一時停止</button><button onClick={() => sendSessionCommand("RESUME")}>再開</button><button onClick={() => sendSpeed(0)}>停止</button><button onClick={() => sendSpeed(1)}>1倍</button><button onClick={() => sendSpeed(10)}>10倍</button><button onClick={() => sendSpeed(60)}>60倍</button></>}<button onClick={chooseAnotherRole}>役割を選び直す</button></div></section>
      <section className="grid stats"><article><span>公開案件</span><strong>{state.jobs.filter(j => j.status === "OPEN").length}</strong><small>全 {state.jobs.length} 件</small></article><article><span>稼働トラック</span><strong>{state.trucks.filter(t => t.status !== "AVAILABLE").length}/{state.trucks.length}</strong><small>車両状態</small></article><article><span>バース</span><strong>{state.docks.filter(d => d.occupied_by).length}/{state.docks.length}</strong><small>占有状況</small></article><article><span>直近通知</span><strong className="small-value">{lastEvent}</strong><small>WebSocket</small></article></section>
      <section className="grid main-grid">
        {state.membership.role === "SHIPPER" && <article className="panel"><div className="panel-title"><h3>案件登録</h3><span className="pill">SHIPPER</span></div><div className="controls"><select value={newJobKind} onChange={event => setNewJobKind(event.target.value as "L" | "B")}><option value="L">往路 L</option><option value="B">帰り荷 B</option></select><input aria-label="案件地点" value={newLocation} onChange={event => setNewLocation(event.target.value)} placeholder="配送地点" /><button onClick={createJob}>案件登録</button></div></article>}
        {state.membership.role === "DRIVER" && <article className="panel"><div className="panel-title"><h3>ドライバー操作</h3><span className="pill">DRIVER</span></div><div className="controls"><label className="truck-picker">車両<select aria-label="車両選択" value={selectedTruckId} onChange={event => setSelectedTruckId(event.target.value)}>{planTrucks.map(truck => <option key={truck.id} value={truck.id}>{truck.name} / {truck.status}</option>)}</select></label><button onClick={createPlan}>計画を生成</button><button onClick={retrievePlan}>計画を取得</button><button onClick={startTrip}>START_TRIP送信</button></div>{plan && <pre className="plan">{JSON.stringify(plan, null, 2)}</pre>}<table><thead><tr><th>種別</th><th>地点</th><th>状態</th><th /></tr></thead><tbody>{state.jobs.filter(job => job.status === "OPEN").map(job => <tr key={job.id}><td><span className={`type type-${job.kind}`}>{job.kind}</span></td><td>{job.location}</td><td>{job.pallets} pallet</td><td><button onClick={() => acceptJob(job)}>案件参加</button></td></tr>)}</tbody></table></article>}
        <article className="panel"><div className="panel-title"><h3>tripsの進捗</h3><span>{state.trips.length}件</span></div>{state.trips.length === 0 ? <p>稼働中のtripはありません。</p> : <table><thead><tr><th>車両</th><th>phase</th><th>状態</th><th>案件</th><th>進捗</th></tr></thead><tbody>{state.trips.map(trip => <tr key={trip.id}><td>{trip.truck_id}</td><td>{trip.phase}</td><td>{trip.status}{trip.wait_reason ? ` / ${trip.wait_reason}` : ""}</td><td>{trip.current_job_id || "帰庫"}</td><td>L {trip.progress.outbound_pallets} / B {trip.progress.return_pallets}</td></tr>)}</tbody></table>}</article>
      </section>
    </>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);

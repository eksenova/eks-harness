import { useEffect, useMemo, useState } from "react";

type Invoice = { id: number; customer: string; amount: number; status: "draft" | "sent" };

declare global {
  interface Window { __ehxState?: (path: string | null) => unknown }
}

const money = (value: number) => value.toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function App() {
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [query, setQuery] = useState("");
  const [customer, setCustomer] = useState("");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("TRY");
  const [archive, setArchive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Invoice | null>(null);
  const [rate, setRate] = useState<number | null>(null);

  useEffect(() => {
    fetch("/api/invoices").then((r) => r.json()).then((data) => setInvoices(data.items));
    fetch("/api/rates").then((r) => r.json()).then((data) => setRate(data.gold));
  }, []);

  useEffect(() => {
    window.__ehxState = (path) => {
      const state: Record<string, unknown> = { invoices, draft: { customer, amount, currency, archive }, query };
      if (!path) return state;
      return path.split(".").reduce<unknown>((node, key) => (node as Record<string, unknown> | undefined)?.[key], state);
    };
  }, [invoices, customer, amount, currency, archive, query]);

  const visible = useMemo(
    () => invoices.filter((invoice) => invoice.customer.toLocaleLowerCase("tr").includes(query.toLocaleLowerCase("tr"))),
    [invoices, query],
  );

  const save = async () => {
    if (!customer.trim() || !(Number(amount) > 0)) {
      setError("Müşteri ve tutar zorunlu");
      return;
    }
    setError(null);
    const response = await fetch("/api/invoices", { method: "POST", body: JSON.stringify({ customer, amount: Number(amount) }) });
    const created = (await response.json()) as Invoice;
    setInvoices((list) => [...list, created]);
    setCustomer("");
    setAmount("");
    setToast(`Fatura #${created.id} kaydedildi`);
    setTimeout(() => setToast(null), 2500);
  };

  const remove = (invoice: Invoice) => {
    setInvoices((list) => list.filter((item) => item.id !== invoice.id));
    setConfirm(null);
    setToast(`Fatura #${invoice.id} silindi`);
    setTimeout(() => setToast(null), 2500);
  };

  return (
    <main>
      <header>
        <h1>Faturalar</h1>
        <span data-testid="rate">Gram altın: {rate === null ? "…" : money(rate)}</span>
      </header>
      <input placeholder="Ara" value={query} onChange={(event) => setQuery(event.target.value)} />
      <ul data-testid="invoice-list">
        {visible.map((invoice) => (
          <li key={invoice.id} data-testid="invoice-row">
            <span className="customer">{invoice.customer}</span>
            <span className="amount">{money(invoice.amount)} ₺</span>
            <span className={`status ${invoice.status}`}>{invoice.status === "sent" ? "Gönderildi" : "Taslak"}</span>
            <button onClick={() => setConfirm(invoice)}>Sil</button>
          </li>
        ))}
      </ul>
      {visible.length === 0 ? <p id="empty-state">Fatura yok</p> : null}
      <section aria-label="Yeni fatura">
        <h2>Yeni fatura</h2>
        <label>Müşteri<input aria-label="Müşteri" value={customer} onChange={(event) => setCustomer(event.target.value)} /></label>
        <label>Tutar<input id="amount" inputMode="decimal" aria-label="Tutar" value={amount} onChange={(event) => setAmount(event.target.value)} /></label>
        <label>Para birimi<select id="currency" value={currency} onChange={(event) => setCurrency(event.target.value)}><option>TRY</option><option>USD</option><option>EUR</option></select></label>
        <label><input type="checkbox" id="e-archive" checked={archive} onChange={(event) => setArchive(event.target.checked)} />e-Arşiv</label>
        {error ? <p role="alert" className="error">{error}</p> : null}
        <button data-testid="save" onClick={save}>Kaydet</button>
      </section>
      {confirm ? (
        <div role="dialog" aria-modal="true" aria-label="Silme onayı" className="dialog">
          <p>#{confirm.id} {confirm.customer} silinsin mi?</p>
          <button onClick={() => remove(confirm)}>Evet, sil</button>
          <button onClick={() => setConfirm(null)}>Vazgeç</button>
        </div>
      ) : null}
      {toast ? <div data-testid="toast" className="toast">{toast}</div> : null}
    </main>
  );
}

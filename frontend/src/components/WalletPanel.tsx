import { gql } from "../api/client";
import { Q } from "../api/queries";
import type { Wallet } from "../api/types";
import { usePolling } from "../hooks/useLive";
import { money, shortId, timeAgo, TXN_STATUS } from "../lib/format";
import { Chip, Panel } from "./ui";

export function WalletPanel({ fleetId, version }: { fleetId: string; version: number }) {
  const { data: wallet, error } = usePolling(
    () => gql<{ wallet: Wallet }>(Q.wallet, { f: fleetId }).then((d) => d.wallet),
    [fleetId, version],
    4000,
  );
  return (
    <Panel title="Billetera de la flota" hint={wallet?.name}>
      {error && <p className="error-text">{error}</p>}
      {wallet && (
        <>
          <div className="balance">{money(wallet.balanceCents)}</div>
          <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
            disponible · {wallet.fleetId}
            {wallet.heldCents > 0 && (
              <span style={{ color: "var(--info)" }}> · {money(wallet.heldCents)} retenidos</span>
            )}
          </div>
          {wallet.transactions.length === 0 && <p className="empty">Sin transacciones.</p>}
          {wallet.transactions.slice(0, 5).map((t) => {
            const st = TXN_STATUS[t.status];
            const amount = t.status === "CAPTURED" ? t.capturedCents : t.amountCents;
            return (
              <div className="txn" key={t.sagaId} title={t.status === "CAPTURED"
                ? `Retenido ${money(t.amountCents)} → cobrado ${money(t.capturedCents)}, liberado ${money(t.releasedCents)}`
                : undefined}>
                <span className="mono">{shortId(t.sagaId, 6)}</span>
                <span>{money(amount)}</span>
                <Chip tone={st.tone} icon={st.icon}>{st.label}</Chip>
                <span className="faint">{timeAgo(t.createdAt)}</span>
              </div>
            );
          })}
        </>
      )}
    </Panel>
  );
}

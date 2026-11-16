import { gql } from "../api/client";
import { Q } from "../api/queries";
import type { Wallet } from "../api/types";
import { usePolling } from "../hooks/useLive";
import { money, shortId, timeAgo } from "../lib/format";
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
            saldo disponible · {wallet.fleetId}
          </div>
          {wallet.transactions.length === 0 && <p className="empty">Sin transacciones.</p>}
          {wallet.transactions.slice(0, 5).map((t) => (
            <div className="txn" key={t.sagaId}>
              <span className="mono">{shortId(t.sagaId, 6)}</span>
              <span>{money(t.amountCents)}</span>
              <Chip tone={t.status === "REFUNDED" ? "warn" : "ok"} icon={t.status === "REFUNDED" ? "↩" : "✓"}>
                {t.status === "REFUNDED" ? "Reembolsado" : "Cobrado"}
              </Chip>
              <span className="faint">{timeAgo(t.createdAt)}</span>
            </div>
          ))}
        </>
      )}
    </Panel>
  );
}

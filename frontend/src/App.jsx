import { useState, useRef, useMemo, useCallback } from "react";
import "./App.css";

const API_URL = `${import.meta.env.VITE_API_URL || "http://localhost:8000"}/api/process-receipt/`;

// Muted "stamp ink" palette for friend avatars — cycles as friends are added
const AVATAR_COLORS = [
  "#B33A3A", // stamp red
  "#3B6E8F", // steel blue
  "#4C7A54", // moss green
  "#B25C82", // dusty mauve
  "#A6763B", // ochre
  "#5C5B8A", // muted violet
  "#3E8E7E", // teal
  "#9C5A3C", // rust brown
];

// Category tag → ledger stamp color + label. Order here also drives the
// order the category chips render in, in the "who's splitting" section.
const TAG_STYLES = {
  staples: { color: "#A6763B", label: "STAPLES" },
  veg: { color: "#4C7A54", label: "VEG" },
  "non-veg": { color: "#A6432D", label: "NON-VEG" },
  drinks: { color: "#3B6E8F", label: "DRINKS" },
  desserts: { color: "#B25C82", label: "DESSERT" },
  unknown: { color: "#7A7568", label: "UNTAGGED" },
};

const CATEGORY_KEYS = Object.keys(TAG_STYLES);

const currency = (n) =>
  `₹${(Number.isFinite(n) ? n : 0).toFixed(2)}`;

function makeId(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function Avatar({ name, color, size = 34 }) {
  const initial = name.trim().charAt(0).toUpperCase() || "?";
  return (
    <span
      className="avatar-bubble"
      title={name}
      style={{ width: size, height: size, background: color }}
    >
      {initial}
    </span>
  );
}

// A small toggleable pill for picking which categories a friend covers.
// Used both in the "add friend" form and in the friend list afterward,
// so assignments can be adjusted any time, not just at add-time.
function CategoryChip({ tagKey, active, onClick }) {
  const style = TAG_STYLES[tagKey];
  return (
    <button
      type="button"
      onClick={onClick}
      className="category-chip"
      style={{
        borderColor: style.color,
        color: active ? "#fff" : style.color,
        backgroundColor: active ? style.color : "transparent",
      }}
    >
      {style.label}
    </button>
  );
}

export default function App() {
  // ---- Friends ----
  const [users, setUsers] = useState([]); 
  const [newUserName, setNewUserName] = useState("");
  // Removed newUserCategories state as requested

  // ---- Receipt items ----
  const [items, setItems] = useState([]); 

  // ---- Upload state ----
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState(null);
  const [receiptReady, setReceiptReady] = useState(false);
  const fileInputRef = useRef(null);

  // ---- Extra charges (Updated for val & pct) ----
  const [extraCharges, setExtraCharges] = useState({
    tax: { val: "", pct: "" },
    tip: { val: "", pct: "" },
    service: { val: "", pct: "" },
  });

  // ---------- Friends logic ----------
  const toggleNewUserCategory = (tagKey) => {
    setNewUserCategories((prev) =>
      prev.includes(tagKey) ? prev.filter((t) => t !== tagKey) : [...prev, tagKey]
    );
  };

  const addUser = (e) => {
    e.preventDefault();
    const name = newUserName.trim();
    if (!name) return;
    const color = AVATAR_COLORS[users.length % AVATAR_COLORS.length];
    setUsers((prev) => [
      ...prev,
      { id: makeId("user"), name, color, categories: [] }, // No initial categories
    ]);
    setNewUserName("");
  };

  const handleSplitEvenly = () => {
    // Get all unique tags currently present on the parsed receipt
    const billTags = [...new Set(items.map((i) => i.tag))];
    if (billTags.length === 0) return;
    
    // Assign all bill tags to every user
    setUsers((prev) =>
      prev.map((u) => ({ ...u, categories: billTags }))
    );
  };

  const handleExtraChange = (key, field, amount) => {
    setExtraCharges((prev) => {
      const updated = { ...prev };
      const num = Number(amount);
      
      if (field === "val") {
        updated[key].val = amount;
        updated[key].pct = subtotal > 0 && amount !== "" ? ((num / subtotal) * 100).toFixed(1) : "";
      } else {
        updated[key].pct = amount;
        updated[key].val = subtotal > 0 && amount !== "" ? ((num / 100) * subtotal).toFixed(2) : "";
      }
      return updated;
    });
  };

  const removeUser = (userId) => {
    setUsers((prev) => prev.filter((u) => u.id !== userId));
  };

  // Lets a friend's categories be adjusted after they've already been added,
  // not just at creation time — same toggle chips, reused inline.
  const toggleUserCategory = (userId, tagKey) => {
    setUsers((prev) =>
      prev.map((u) =>
        u.id !== userId
          ? u
          : {
              ...u,
              categories: u.categories.includes(tagKey)
                ? u.categories.filter((t) => t !== tagKey)
                : [...u.categories, tagKey],
            }
      )
    );
  };

  // ---------- Upload logic ----------
  const uploadReceipt = useCallback(async (file) => {
    if (!file) return;
    setUploadError(null);
    setIsUploading(true);
    setReceiptReady(false);

    try {
      const formData = new FormData();
      formData.append("receipt_image", file);

      const res = await fetch(API_URL, { method: "POST", body: formData });
      const data = await res.json();

      if (!res.ok || !data.success) {
        throw new Error(data.error || "The backend could not read this receipt.");
      }

      const parsedItems = (data.items || []).map((it) => ({
        id: makeId("item"),
        name: it.name,
        price: Number(it.price) || 0,
        tag: TAG_STYLES[it.tag] ? it.tag : "unknown",
      }));

      setItems(parsedItems);

      // 1. Calculate the subtotal locally so we can use it immediately 
      // for the percentage math before the next React render cycle.
      const currentSubtotal = parsedItems.reduce((sum, item) => sum + item.price, 0);

      // 2. Helper function to calculate percentage on the fly
      const calculatePct = (amount) => {
        return currentSubtotal > 0 && amount > 0 
          ? ((Number(amount) / currentSubtotal) * 100).toFixed(1) 
          : "";
      };

      // 3. Auto-fill both the value and the newly calculated percentage
      const detected = data.charges || {};
      setExtraCharges({
        tax: { 
          val: detected.tax > 0 ? String(detected.tax) : "", 
          pct: calculatePct(detected.tax) 
        },
        tip: { 
          val: detected.tip > 0 ? String(detected.tip) : "", 
          pct: calculatePct(detected.tip) 
        },
        service: { 
          val: detected.service > 0 ? String(detected.service) : "", 
          pct: calculatePct(detected.service) 
        },
      });

      setReceiptReady(true);
    } catch (err) {
      setUploadError(err.message || "Something went wrong while uploading.");
    } finally {
      setIsUploading(false);
    }
  }, []);

  const handleDrop = (e) => {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files?.[0];
    uploadReceipt(file);
  };

  const handleBrowse = (e) => {
    const file = e.target.files?.[0];
    uploadReceipt(file);
    e.target.value = ""; // allow re-selecting the same file later
  };

  // ---------- Derived assignment: which friends cover which item ----------
  // Rather than storing sharedBy on each item, we derive it live from each
  // friend's chosen categories. This keeps state flat (items never need to
  // be touched when a friend's categories change) and means assigning a
  // category once instantly applies to every matching dish on the bill.
  const itemAssignments = useMemo(() => {
    const map = {};
    items.forEach((item) => {
      map[item.id] = users.filter((u) => u.categories.includes(item.tag));
    });
    return map;
  }, [items, users]);

  // ---------- Derived totals ----------
  const subtotal = useMemo(
    () => items.reduce((sum, item) => sum + item.price, 0),
    [items]
  );

  const extraTotal = useMemo(() => {
    const t = Number(extraCharges.tax.val) || 0;
    const p = Number(extraCharges.tip.val) || 0;
    const s = Number(extraCharges.service.val) || 0;
    return t + p + s;
  }, [extraCharges]);

  const grandTotal = subtotal + extraTotal;

  // Per-user totals: item share (via category match) + a proportional
  // slice of tax/tip/service based on how much of the subtotal they cover.
  const userTotals = useMemo(() => {
    const base = {};
    users.forEach((u) => (base[u.id] = 0));

    items.forEach((item) => {
      const assigned = itemAssignments[item.id] || [];
      if (assigned.length === 0) return;
      const share = item.price / assigned.length;
      assigned.forEach((u) => {
        base[u.id] += share;
      });
    });

    if (subtotal > 0) {
      users.forEach((u) => {
        const proportion = base[u.id] / subtotal;
        base[u.id] += proportion * extraTotal;
      });
    }

    return base;
  }, [items, users, itemAssignments, subtotal, extraTotal]);

  const unassignedTotal = useMemo(() => {
    const assigned = items.reduce((sum, item) => {
      const assignedUsers = itemAssignments[item.id] || [];
      if (assignedUsers.length === 0) return sum;
      return sum + item.price;
    }, 0);
    return subtotal - assigned;
  }, [items, itemAssignments, subtotal]);

  return (
    <div className="app-shell">
      <header className="brand-header">
        <span className="brand-mark">SPLIT IT</span>
        <span className="brand-tagline">split the bill</span>
      </header>

      <main className="receipt-paper">
        <div className="perf-top" aria-hidden="true" />

        {/* ---------- Section 1: Upload ---------- */}
        <section className="ledger-section">
          <h2 className="ledger-heading">01 · Upload</h2>

          <div
            className={`dropzone ${isDragging ? "dropzone-active" : ""} ${
              isUploading ? "dropzone-busy" : ""
            }`}
            onDragOver={(e) => {
              e.preventDefault();
              setIsDragging(true);
            }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={handleDrop}
            onClick={() => !isUploading && fileInputRef.current?.click()}
          >
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              hidden
              onChange={handleBrowse}
            />

            {isUploading ? (
              <div className="dropzone-state">
                <span className="spinner" aria-hidden="true" />
                <p>reading...</p>
              </div>
            ) : receiptReady ? (
              <div className="dropzone-state">
                <span className="stamp">✓</span>
                <p className="dropzone-hint">drag new photo / click to browse</p>
              </div>
            ) : (
              <div className="dropzone-state">
                <p>drag a photo / click to browse</p>
              </div>
            )}
          </div>

          {uploadError && <p className="error-text">⚠ {uploadError}</p>}
        </section>

        <div className="divider" />

        {/* ---------- Section 2: Friends ---------- */}
        <section className="ledger-section">
          <h2 className="ledger-heading">02 · Friends</h2>

          <form className="add-user-form" onSubmit={addUser}>
            <div className="add-user-form-row">
              <input
                type="text"
                placeholder="name"
                value={newUserName}
                onChange={(e) => setNewUserName(e.target.value)}
                className="text-input"
              />
              <button type="submit" className="btn-add">ADD</button>
            </div>
          </form>

          {users.length === 0 ? (
            <p className="empty-hint">no friends added.</p>
          ) : (
            <>
              <ul className="friend-list">
                {users.map((u) => (
                  <li key={u.id} className="friend-row">
                    <div className="friend-row-header">
                      <Avatar name={u.name} color={u.color} />
                      <span className="friend-name">{u.name}</span>
                      <button
                        type="button"
                        className="btn-remove"
                        onClick={() => removeUser(u.id)}
                      >✕</button>
                    </div>
                    <div className="category-chip-row">
                      {CATEGORY_KEYS.map((tagKey) => (
                        <CategoryChip
                          key={tagKey}
                          tagKey={tagKey}
                          active={u.categories.includes(tagKey)}
                          onClick={() => toggleUserCategory(u.id, tagKey)}
                        />
                      ))}
                    </div>
                  </li>
                ))}
              </ul>
              <button 
                type="button" 
                className="btn-secondary" 
                onClick={handleSplitEvenly}
                style={{ marginTop: '12px' }}
              >
                Split evenly
              </button>
            </>
          )}
        </section>

        <div className="divider" />

        {/* ---------- Section 3: Receipt items ---------- */}
        <section className="ledger-section">
          <h2 className="ledger-heading">03 · Receipt Contents</h2>

          {items.length === 0 ? (
            <p className="empty-hint">
              Nothing here yet — upload a receipt to populate this ledger.
            </p>
          ) : (
            <ul className="item-list">
              {items.map((item) => {
                const tagStyle = TAG_STYLES[item.tag] || TAG_STYLES.unknown;
                const assignedUsers = itemAssignments[item.id] || [];
                const perPerson =
                  assignedUsers.length > 0 ? item.price / assignedUsers.length : 0;

                return (
                  <li key={item.id} className="item-row">
                    <div className="item-line">
                      <span className="item-name">{item.name}</span>
                      <span className="item-leader" aria-hidden="true" />
                      <span className="item-price">{currency(item.price)}</span>
                    </div>

                    <span
                      className="tag-pill"
                      style={{
                        color: tagStyle.color,
                        borderColor: tagStyle.color,
                      }}
                    >
                      {tagStyle.label}
                    </span>

                    {assignedUsers.length === 0 ? (
                      <p className="per-person-hint">
                        No one covers "{tagStyle.label}" yet — tag a friend with
                        this category above.
                      </p>
                    ) : (
                      <>
                        <div className="assign-row">
                          {assignedUsers.map((u) => (
                            <Avatar key={u.id} name={u.name} color={u.color} size={28} />
                          ))}
                        </div>
                        <p className="per-person-hint">
                          {currency(perPerson)} each · split {assignedUsers.length} way
                          {assignedUsers.length > 1 ? "s" : ""}
                        </p>
                      </>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <div className="divider" />

        {/* ---------- Section 4: Extra Charges ---------- */}
        <section className="ledger-section">
          <h2 className="ledger-heading">04 · Extras</h2>
          <div className="extra-charges-list">
            {["tax", "tip", "service"].map((key) => (
              <div key={key} className="extra-charge-row">
                <span>{key}</span>
                <div className="extra-input-group">
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    placeholder="₹0.00"
                    value={extraCharges[key].val}
                    onChange={(e) => handleExtraChange(key, "val", e.target.value)}
                    className="text-input"
                  />
                  <input
                    type="number"
                    min="0"
                    step="0.1"
                    placeholder="% 0.0"
                    value={extraCharges[key].pct}
                    onChange={(e) => handleExtraChange(key, "pct", e.target.value)}
                    className="text-input"
                  />
                </div>
              </div>
            ))}
          </div>
        </section>

        <div className="divider" />

        {/* ---------- Section 5: Summary ---------- */}
        <section className="ledger-section">
          <h2 className="ledger-heading">05 · Final Tally</h2>

          <div className="summary-line">
            <span>Subtotal</span>
            <span className="item-leader" aria-hidden="true" />
            <span>{currency(subtotal)}</span>
          </div>
          <div className="summary-line">
            <span>Tax + Tip + Service</span>
            <span className="item-leader" aria-hidden="true" />
            <span>{currency(extraTotal)}</span>
          </div>
          <div className="summary-line summary-total">
            <span>Grand Total</span>
            <span className="item-leader" aria-hidden="true" />
            <span>{currency(grandTotal)}</span>
          </div>

          {unassignedTotal > 0.004 && (
            <p className="error-text small">
              ⚠ {currency(unassignedTotal)} of items are still unassigned.
            </p>
          )}

          {users.length > 0 && (
            <ul className="user-total-list">
              {users.map((u) => (
                <li key={u.id} className="user-total-row">
                  <Avatar name={u.name} color={u.color} size={26} />
                  <span className="friend-name">{u.name}</span>
                  <span className="item-leader" aria-hidden="true" />
                  <span className="user-total-amount">{currency(userTotals[u.id] || 0)}</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <div className="perf-bottom" aria-hidden="true" />
      </main>
    </div>
  );
}
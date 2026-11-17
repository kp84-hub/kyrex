import React, { useEffect, useState } from 'react';
import { listOpenCodeGoModels } from '../lib/api.js';
import './BotModelPicker.css';

const compact = (value) => value.toLowerCase().replace(/[^a-z0-9]/g, '');

function matches(models, query) {
  const words = query.toLowerCase().match(/[a-z0-9]+/g) || [];
  const whole = words.join('');
  if (!whole) return models.slice(0, 10);
  const matching = models.flatMap((id) => {
    const model = compact(id);
    if (!words.every(word => model.includes(word))) return [];
    const rank = model === whole ? 0 : model.startsWith(whole) ? 1 : 2;
    return [{ id, rank }];
  });
  return matching.sort((a, b) => a.rank - b.rank).slice(0, 10).map(({ id }) => id);
}

export default function BotModelPicker({ id, profile, value, onChange, disabled }) {
  const [open, setOpen] = useState(false);
  const [filtering, setFiltering] = useState(false);
  const [active, setActive] = useState(-1);
  const [goModels, setGoModels] = useState(null);
  const isGo = profile?.opencode_go === true;
  useEffect(() => {
    if (!open || !isGo || goModels) return;
    let current = true;
    listOpenCodeGoModels().then(models => {
      if (current && models.length > 0) {
        setGoModels(models);
        setActive(-1);
      }
    }).catch(() => {}); // The provider profile's saved suggestions are still usable.
    return () => { current = false; };
  }, [open, isGo, goModels]);

  const models = (isGo && goModels) || profile?.models || [];
  const options = matches(models, filtering ? value : '');
  const visible = open && !disabled && options.length > 0;
  const listId = `${id}-suggestions`;

  const select = (model) => {
    onChange(model);
    setOpen(false);
    setActive(-1);
  };

  const onKeyDown = (event) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === 'Escape' && visible) {
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      setActive(-1);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (!options.length) return;
      event.preventDefault();
      setOpen(true);
      const next = event.key === 'ArrowDown' ? Math.min(active + 1, options.length - 1)
        : active <= 0 ? options.length - 1 : active - 1;
      setActive(next);
      document.getElementById(`${listId}-${next}`)?.scrollIntoView?.({ block: 'nearest' });
    } else if (event.key === 'Enter' && visible && (active >= 0 || filtering)) {
      event.preventDefault();
      select(options[active] || options[0]);
    }
  };

  return <div className="bot-model-picker">
    <input
      id={id}
      type="text"
      role="combobox"
      aria-autocomplete="list"
      aria-expanded={visible}
      aria-controls={visible ? listId : undefined}
      aria-activedescendant={visible && options[active] ? `${listId}-${active}` : undefined}
      autoComplete="off"
      spellCheck={false}
      disabled={disabled}
      placeholder="Search or enter a model ID"
      value={value}
      onFocus={() => { setOpen(true); setFiltering(false); setActive(-1); }}
      onBlur={() => setOpen(false)}
      onKeyDown={onKeyDown}
      onChange={(event) => {
        onChange(event.target.value);
        setFiltering(true);
        setOpen(true);
        setActive(-1);
      }}
    />
    {visible && <ul id={listId} role="listbox" aria-label="Model suggestions">
      {options.map((model, index) => <li
        id={`${listId}-${index}`}
        key={model}
        role="option"
        aria-selected={active === index}
        onMouseDown={event => event.preventDefault()}
        onClick={() => select(model)}
      >{model}</li>)}
    </ul>}
  </div>;
}

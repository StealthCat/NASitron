# UI preview

The interface originally developed on `ui-preview` is now merged into `main`,
alongside all features from `preview`. It refines NASitron's shared interface while retaining the
existing dark navy palette, `#FC1859` accent, and health-status colors.

- Cleaner navigation, page headings, typography, cards, forms, tabs, and tables.
- Original SVG storage illustration, server rack graphics, and section icons.
- Dashboard storage footprint using reported pool allocation, with pool coverage
  shown below the capacity ring. This represents physical pool allocation, not
  dataset-available space.
- Server cards with pool capacity and clear host identities. Pool capacity rows
  identify the host when pool names repeat across servers.
- Historical charts with timestamp-based spacing, sample readouts, keyboard
  inspection, and explicit loading-failure and empty states.
- Responsive layouts, keyboard-accessible table scrolling, skip navigation,
  mobile-menu focus management, and reduced-motion support.

## Run the merged interface

Use the normal setup in the [README](../README.md), then build `main`:

```bash
git fetch origin
git switch main
docker compose up -d --build
```

## Validation

The existing Python suite and capacity-display regression tests pass. Rendered
checks cover the dashboard, servers, pools, tanks, datasets, drives, alerts,
maintenance, settings, users, add-server form, and server detail at widths of
1440, 1024, 768, 390, and 320 pixels. The visual fixture includes three hosts and
a 25-drive server. Checks also cover empty onboarding, login, dataset filtering,
chart keyboard navigation, mobile-menu focus, and reduced motion.

All illustrations and icons are served locally; no external font, image service,
or chart library is required.

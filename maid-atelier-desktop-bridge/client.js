/**
 * maid-atelier-desktop-bridge — Client half.
 *
 * WHY THIS EXISTS
 * maid-atelier (0.1.7) mounts its palace backdrop and both maid figures by
 * looking up `document.querySelector("[class*='centerCol']")` — the column of
 * @deepseek-ai/dsh-client-ui-layout. DSH Desktop's `advanced` and `extended`
 * window modes replace that layout with a desktop-owned root frame
 * (`DesktopOwnedFrame`) whose conversation column is
 * `main.dshDesktopConversationSurface` and which contains no `centerCol` at
 * all. `ensureChatAreaStage()` therefore returns false forever, the whole
 * artwork layer is never inserted into the DOM, and the conversation surface
 * shows its own `--dsw-alias-bg-base` (transparent) on top of the window
 * background — a black column.
 *
 * WHAT THIS DOES
 * Re-creates the exact skin-owned chrome elements the skin would have mounted
 * (`character-stage`, `top-trim`, `bottom-trim`) inside the desktop
 * conversation column, so the skin's own stylesheet styles them unchanged: it
 * is keyed only on `body[data-dsh-maid-atelier]` plus `data-skin-chrome`,
 * `data-maid-character` and `data-skin-trim-layer`, never on the parent. The
 * palace art is read back through the `--maid-palace-art` custom property the
 * skin already writes on `body`, so light/dark follows the skin for free.
 *
 * COUPLING
 * This deliberately mirrors maid-atelier's DOM contract. It is a stop-gap for
 * an upstream defect (Small-tailqwq/dsh-deep-whale#160); the real fix is for
 * the skin to accept the desktop column as a mount target. The bridge is inert
 * while the skin is off and detaches itself the moment the skin mounts a stage
 * of its own (e.g. after switching to compatibility mode or installing a fixed
 * skin), so it cannot double-mount.
 */
window.__ModuleLoader__.load({
  id: '@local/maid-atelier-desktop-bridge',
  factory() {
    /** Marker separating the elements this bridge owns from the skin's own. */
    const OWN = 'data-maid-bridge';
    const OWNER = 'maid-atelier-desktop-bridge';
    /** Column supplied by dsh-plugin-desktop's advanced/extended frame. */
    const DESKTOP_COLUMN = '.dshDesktopConversationSurface';
    /** The skin reaches the conversation phase root through centerCol; we cannot. */
    const PHASE_SELECTOR =
      "[data-phase='hero'], [data-phase='active'], [data-phase='settling']";

    /** Values mirrored from maid-atelier's lib/client.js. */
    const LEFT_SELECTOR = "[data-maid-character='left']";
    const RIGHT_SELECTOR = "[data-maid-character='right']";
    const CROWDED_ATTRIBUTE = 'data-maid-figures-crowded';
    const CROWDED_GAP_PX = 32;
    const RESTORE_GAP_PX = 96;
    const ART_BASE = 'skin-assets/maid-atelier/';
    const ART = {
      left: '2818e5359596b13c5d64f5b1c5d4a9e0c05be477824779af8af37a0e10016b07.webp',
      right: '7c5a5493e84418bc0435013c8605222718923872c15eb46fdd36158d201c0b1e.webp',
      vision: '98cf9484ee96018b1ddc6d61f30945ce973158002fe31a2be0790e44c6718515.webp',
    };

    /**
     * The skin attaches both rules to `[class*=centerCol]`. Without them the
     * stage (position:absolute, z-index:0) would cover the composer, because a
     * positioned z-index:0 box paints above in-flow content.
     */
    const CSS = `
body[data-dsh-maid-atelier] .dshDesktopConversationSurface { position: relative; }
body[data-dsh-maid-atelier] .dshDesktopConversationSurface :is(${PHASE_SELECTOR}) { position: relative; }
`;

    const artUrl = (file) => new URL(`${ART_BASE}${file}`, document.baseURI).href;

    /** Build the skin's character stage: palace backdrop plus both maids. */
    function createStage() {
      const stage = document.createElement('div');
      stage.dataset.skinChrome = 'character-stage';
      stage.dataset.skinOwner = OWNER;
      stage.setAttribute(OWN, '');
      stage.setAttribute('aria-hidden', 'true');
      for (const [key, file] of Object.entries(ART)) {
        const image = document.createElement('img');
        image.dataset.maidCharacter = key;
        image.alt = '';
        image.src = artUrl(file);
        stage.append(image);
      }
      return stage;
    }

    function createTopTrim() {
      const trim = document.createElement('div');
      trim.dataset.skinChrome = 'top-trim';
      trim.dataset.skinOwner = OWNER;
      trim.setAttribute(OWN, '');
      trim.setAttribute('aria-hidden', 'true');
      for (const layer of ['landing', 'workspace']) {
        const element = document.createElement('div');
        element.dataset.skinTrimLayer = layer;
        trim.append(element);
      }
      return trim;
    }

    function createBottomTrim() {
      const trim = document.createElement('div');
      trim.dataset.skinChrome = 'bottom-trim';
      trim.dataset.skinOwner = OWNER;
      trim.setAttribute(OWN, '');
      trim.setAttribute('aria-hidden', 'true');
      return trim;
    }

    return {
      apply(ctx) {
        const body = document.body;
        const stage = createStage();
        const topTrim = createTopTrim();
        const bottomTrim = createBottomTrim();

        let frame = null;
        let crowded = false;
        let crowdFrame = null;
        let resizeObserver;

        const skinActive = () => body.hasAttribute('data-dsh-maid-atelier');
        /** A stage the skin mounted itself — if present we must stand down. */
        const skinOwnsStage = () => document.querySelector(
          `[data-skin-chrome='character-stage']:not([${OWN}])`,
        ) !== null;

        const decideCrowded = (next) => {
          if (next === crowded) return;
          crowded = next;
          if (next) body.setAttribute(CROWDED_ATTRIBUTE, '');
          else body.removeAttribute(CROWDED_ATTRIBUTE);
        };

        /** Mirrors maid-atelier's installMaidCharacterFit for a bridged stage. */
        const measure = () => {
          crowdFrame = null;
          if (stage.parentElement === null || stage.offsetWidth === 0) return;
          const left = stage.querySelector(LEFT_SELECTOR);
          const right = stage.querySelector(RIGHT_SELECTOR);
          if (left === null || right === null) return;
          const gap = right.offsetLeft - left.offsetLeft - left.offsetWidth;
          decideCrowded(crowded ? gap < RESTORE_GAP_PX : gap < CROWDED_GAP_PX);
        };
        const scheduleMeasure = () => {
          if (crowdFrame === null) crowdFrame = requestAnimationFrame(measure);
        };

        const detach = () => {
          if (stage.parentElement !== null) stage.remove();
          if (topTrim.parentElement !== null) topTrim.remove();
          if (bottomTrim.parentElement !== null) bottomTrim.remove();
          decideCrowded(false);
        };

        const sync = () => {
          frame = null;
          const column = document.querySelector(DESKTOP_COLUMN);
          if (!skinActive() || skinOwnsStage() || column === null) {
            detach();
            return;
          }
          // Same placement the skin uses inside centerCol: stage first, trims last.
          if (column.firstChild !== stage) column.prepend(stage);
          if (topTrim.parentElement !== column) column.append(topTrim);
          if (bottomTrim.parentElement !== column) column.append(bottomTrim);
          scheduleMeasure();
        };
        const schedule = () => {
          if (frame === null) frame = requestAnimationFrame(sync);
        };

        ctx.effect(() => {
          const style = document.createElement('style');
          style.setAttribute('data-maid-bridge-styles', '');
          style.textContent = CSS;
          document.head.append(style);

          if (typeof ResizeObserver === 'function') {
            resizeObserver = new ResizeObserver(scheduleMeasure);
            resizeObserver.observe(stage);
          }
          window.addEventListener('resize', scheduleMeasure, { passive: true });

          const observer = new MutationObserver(schedule);
          observer.observe(body, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['data-dsh-maid-atelier', 'data-ds-dark-theme'],
          });
          schedule();

          return () => {
            observer.disconnect();
            resizeObserver?.disconnect();
            resizeObserver = undefined;
            window.removeEventListener('resize', scheduleMeasure);
            if (frame !== null) cancelAnimationFrame(frame);
            if (crowdFrame !== null) cancelAnimationFrame(crowdFrame);
            frame = null;
            crowdFrame = null;
            decideCrowded(false);
            style.remove();
            stage.remove();
            topTrim.remove();
            bottomTrim.remove();
          };
        }, 'maid-atelier desktop bridge: artwork re-mount and column fit');
      },
    };
  },
});

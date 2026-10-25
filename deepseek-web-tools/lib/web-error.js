/**
 * A local stand-in for `@deepseek-ai/dsh-web`'s `WebError`.
 *
 * Why this exists: a workspace bundle installed with `plugin_manager`'s
 * `install_bundle` is a `link:` dependency, so its files live outside the
 * profile's `node_modules`. The Desktop profile-package resolver only maps bare
 * `@deepseek-ai/*` specifiers for packages that sit inside the profile, and a
 * link-installed bundle therefore cannot import Harness packages at all (proven
 * with two probe bundles: a zero-import row activates, the same row plus one
 * `@deepseek-ai/schemastery` import fails to import). Every `@local/*` bundle in
 * this profile follows the same zero-import convention.
 *
 * The `ctx.web` contract asks providers to throw a `WebError`, whose observable
 * contract is a stable machine-routable `code` (consumers route on `code`, and
 * the shipped tool layer does not `instanceof`-check provider errors). This class
 * reproduces that contract locally, so routing and messages stay identical while
 * the bundle stays loadable from any directory and independent of private
 * `@deepseek-ai/*` package paths across DSH upgrades.
 *
 * @module @local/dsh-deepseek-web-tools/web-error
 */

/**
 * Error carrying a stable machine-routable `code`, mirroring the Harness base
 * error (`name`, `message`, `code`, `cause`) without importing it.
 */
export class WebError extends Error {
  /**
   * @param {string} message - human-readable detail.
   * @param {string} code - stable failure class; consumers route on this.
   * @param {{ cause?: unknown }} [options] - standard error options.
   */
  constructor(message, code, options) {
    super(message, options)
    /** Stable machine-routable failure class. */
    this.code = code
    this.name = 'WebError'
  }
}

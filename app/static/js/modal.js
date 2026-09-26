// Minimal Pico-compatible <dialog> modal helper: any element with
// data-target="<dialog id>" toggles that dialog open/closed, with the
// modal-is-open/-opening/-closing classes Pico's CSS animates against.
(function () {
    const OPEN_CLASS = "modal-is-open";
    const OPENING_CLASS = "modal-is-opening";
    const CLOSING_CLASS = "modal-is-closing";
    const ANIMATION_MS = 300;
    let visibleModal = null;

    function openModal(modal) {
        const html = document.documentElement;
        html.classList.add(OPEN_CLASS, OPENING_CLASS);
        modal.showModal();
        setTimeout(() => {
            visibleModal = modal;
            html.classList.remove(OPENING_CLASS);
        }, ANIMATION_MS);
    }

    function closeModal(modal) {
        visibleModal = null;
        const html = document.documentElement;
        html.classList.add(CLOSING_CLASS);
        setTimeout(() => {
            html.classList.remove(CLOSING_CLASS, OPEN_CLASS);
            modal.close();
        }, ANIMATION_MS);
    }

    document.addEventListener("click", (event) => {
        const trigger = event.target.closest("[data-target]");
        if (trigger) {
            const modal = document.getElementById(trigger.dataset.target);
            if (modal) {
                event.preventDefault();
                modal.hasAttribute("open") ? closeModal(modal) : openModal(modal);
            }
            return;
        }
        if (visibleModal && !visibleModal.querySelector("article").contains(event.target)) {
            closeModal(visibleModal);
        }
    });

    document.addEventListener("cancel", (event) => {
        if (event.target.tagName === "DIALOG") {
            event.preventDefault();
            closeModal(event.target);
        }
    }, true);
})();

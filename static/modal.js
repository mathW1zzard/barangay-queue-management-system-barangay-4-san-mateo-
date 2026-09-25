// Wait for the HTML to fully load before attaching event listeners
document.addEventListener('DOMContentLoaded', () => {

    const addModal = document.querySelector("#add-modal");
    const closeAddModal = document.querySelector(".add-btn"); 

    if (closeAddModal && addModal) {
        // Open Modal
        closeAddModal.onclick = () => addModal.showModal();

        // Click Outside to Close
        addModal.onclick = (e) => {
            if (e.target === addModal) {
                addModal.close();
            }
        };
    }

    const editModal = document.querySelector("#edit-modal-{{ service.id }}");

    if (editModal) {
        // Adding the same click-outside logic for consistency
        editModal.onclick = (e) => {
            if (e.target === editModal) {
                editModal.close();
            }
        };
    }

    const deleteModal = document.querySelector("#delete-modal-{{ service.id }}");

    if (deleteModal) {
        // Adding the same click-outside logic for consistency
        deleteModal.onclick = (e) => {
            if (e.target === deleteModal) {
                deleteModal.close();
            }
        };
    }
})